#pragma once

#include <IO/ReadBufferFromFileBase.h>
#include <IO/ReadSettings.h>
#include <Common/PageCache.h>

namespace DB
{

class CachedInMemoryReadBufferFromFile : public ReadBufferFromFileBase
{
public:
    /// `in_` must support using external buffer. I.e. we assign its internal_buffer before each next()
    /// call and expect the read data to be put into that buffer.
    /// `in_` should be seekable and should be able to read the whole file from 0 to in_->getFileSize();
    /// in particular, don't call setReadUntilPosition() on `in_` directly, call
    /// CachedInMemoryReadBufferFromFile::setReadUntilPosition().
    CachedInMemoryReadBufferFromFile(PageCacheKey cache_key_, PageCachePtr cache_, std::unique_ptr<ReadBufferFromFileBase> in_, const ReadSettings & settings_, bool restricted_seek_);

    String getFileName() const override;
    off_t seek(off_t off, int whence) override;
    off_t getPosition() override;
    size_t getFileOffsetOfBufferEnd() const override;
    bool supportsRightBoundedReads() const override { return true; }
    void setReadUntilPosition(size_t position) override;
    void setReadUntilEnd() override;

private:
    PageCacheKey cache_key; // .offset is offset of `chunk` start
    PageCachePtr cache;
    size_t block_size;
    ReadSettings settings;
    std::unique_ptr<ReadBufferFromFileBase> in;

    size_t file_offset_of_buffer_end = 0;
    size_t read_until_position;

    PageCache::MappedPtr chunk;

    bool restricted_seek;

    bool nextImpl() override;
};

}
