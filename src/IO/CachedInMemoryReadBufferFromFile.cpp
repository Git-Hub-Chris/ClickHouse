#include "CachedInMemoryReadBufferFromFile.h"
#include <IO/SwapHelper.h>
#include <base/scope_guard.h>
#include <Common/logger_useful.h>

namespace DB
{

namespace ErrorCodes
{
    extern const int UNEXPECTED_END_OF_FILE;
    extern const int CANNOT_SEEK_THROUGH_FILE;
    extern const int SEEK_POSITION_OUT_OF_BOUND;
}

CachedInMemoryReadBufferFromFile::CachedInMemoryReadBufferFromFile(
    PageCacheKey cache_key_, PageCachePtr cache_, std::unique_ptr<ReadBufferFromFileBase> in_, const ReadSettings & settings_, bool restricted_seek_)
    : ReadBufferFromFileBase(0, nullptr, 0, in_->getFileSize()), cache_key(cache_key_), cache(cache_), block_size(cache->defaultBlockSize()), settings(settings_), in(std::move(in_))
    , read_until_position(file_size.value()), restricted_seek(restricted_seek_)
{
    cache_key.offset = 0;
}

String CachedInMemoryReadBufferFromFile::getFileName() const
{
    return in->getFileName();
}

off_t CachedInMemoryReadBufferFromFile::seek(off_t off, int whence)
{
    if (whence != SEEK_SET)
        throw Exception(ErrorCodes::CANNOT_SEEK_THROUGH_FILE, "Only SEEK_SET mode is allowed.");

    if (chunk && restricted_seek)
    {
        throw Exception(
            ErrorCodes::CANNOT_SEEK_THROUGH_FILE,
            "Seek is allowed only before first read attempt from the buffer (current offset: "
            "{}, new offset: {}, reading until position: {}, available: {})",
            getPosition(), off, read_until_position, available());
    }

    size_t offset = static_cast<size_t>(off);
    if (offset > file_size.value())
        throw Exception(ErrorCodes::SEEK_POSITION_OUT_OF_BOUND, "Seek position is out of bounds. Offset: {}", off);

    if (offset >= file_offset_of_buffer_end - working_buffer.size() && offset <= file_offset_of_buffer_end)
    {
        pos = working_buffer.end() - (file_offset_of_buffer_end - offset);
        chassert(getPosition() == off);
        return off;
    }

    resetWorkingBuffer();

    file_offset_of_buffer_end = offset;
    chunk.reset();

    chassert(getPosition() == off);
    return off;
}

off_t CachedInMemoryReadBufferFromFile::getPosition()
{
    return file_offset_of_buffer_end - available();
}

size_t CachedInMemoryReadBufferFromFile::getFileOffsetOfBufferEnd() const
{
    return file_offset_of_buffer_end;
}

void CachedInMemoryReadBufferFromFile::setReadUntilPosition(size_t position)
{
    read_until_position = position;
    if (position < static_cast<size_t>(getPosition()))
    {
        resetWorkingBuffer();
        chunk.reset();
    }
    else if (position < file_offset_of_buffer_end)
    {
        size_t diff = file_offset_of_buffer_end - position;
        working_buffer.resize(working_buffer.size() - diff);
        file_offset_of_buffer_end -= diff;
    }
}

void CachedInMemoryReadBufferFromFile::setReadUntilEnd()
{
    setReadUntilPosition(file_size.value());
}

bool CachedInMemoryReadBufferFromFile::nextImpl()
{
    chassert(read_until_position <= file_size.value());
    if (file_offset_of_buffer_end >= read_until_position)
        return false;

    if (chunk != nullptr && file_offset_of_buffer_end >= cache_key.offset + block_size)
    {
        chassert(file_offset_of_buffer_end == cache_key.offset + block_size);
        chunk.reset();
    }

    if (chunk == nullptr)
    {
        cache_key.offset = file_offset_of_buffer_end / block_size * block_size;
        cache_key.size = std::min(block_size, file_size.value() - cache_key.offset);

        chunk = cache->getOrSet(cache_key, settings.read_from_page_cache_if_exists_otherwise_bypass_cache, settings.page_cache_inject_eviction, [&](auto cell)
        {
            Buffer prev_in_buffer = in->internalBuffer();
            SCOPE_EXIT({ in->set(prev_in_buffer.begin(), prev_in_buffer.size()); });

            size_t pos = 0;
            while (pos < cache_key.size)
            {
                char * piece_start = cell->data() + pos;
                size_t piece_size = cache_key.size - pos;
                in->set(piece_start, piece_size);
                if (pos == 0)
                    in->seek(cache_key.offset, SEEK_SET);
                else
                    chassert(!in->available());

                if (in->eof())
                    throw Exception(ErrorCodes::UNEXPECTED_END_OF_FILE, "File {} ended after {} bytes, but we expected {}",
                        getFileName(), cache_key.offset + pos, file_size.value());

                chassert(in->position() >= piece_start && in->buffer().end() <= piece_start + piece_size);
                chassert(in->getPosition() == static_cast<off_t>(cache_key.offset + pos));

                size_t n = in->available();
                chassert(n);
                if (in->position() != piece_start)
                    memmove(piece_start, in->position(), n);
                in->position() += n;
                pos += n;
            }

            return cell;
        });
    }

    nextimpl_working_buffer_offset = file_offset_of_buffer_end - cache_key.offset;
    working_buffer = Buffer(
        chunk->data(),
        chunk->data() + std::min(chunk->size(), read_until_position - cache_key.offset));
    pos = working_buffer.begin() + nextimpl_working_buffer_offset;

    if (!internal_buffer.empty())
    {
        /// We were given an external buffer to read into. Copy the data into it.
        /// Would be nice to avoid this copy, somehow, maybe by making ReadBufferFromRemoteFSGather
        /// and AsynchronousBoundedReadBuffer explicitly aware of the page cache.
        size_t n = std::min(available(), internal_buffer.size());
        memcpy(internal_buffer.begin(), pos, n);
        working_buffer = Buffer(internal_buffer.begin(), internal_buffer.begin() + n);
        pos = working_buffer.begin();
        nextimpl_working_buffer_offset = 0;
    }

    file_offset_of_buffer_end += available();

    return true;
}

}
