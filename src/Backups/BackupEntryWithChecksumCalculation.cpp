#include <Backups/BackupEntryWithChecksumCalculation.h>
#include <IO/HashingReadBuffer.h>


namespace DB
{

namespace ErrorCodes
{
    extern const int UNEXPECTED_END_OF_FILE;
}

namespace
{
    /// We mix the checksum calculated for non-encrypted data with IV generated to encrypt the file
    /// to generate kind of a checksum for encrypted data. Of course it differs from the CityHash properly calculated for encrypted data.
    UInt128 combineChecksums(UInt128 checksum1, UInt128 second_checksum)
    {
        chassert(std::size(second_checksum.items) == 2);
        return CityHash_v1_0_2::CityHash128WithSeed(reinterpret_cast<const char *>(&checksum1), sizeof(checksum1), {second_checksum.items[0], second_checksum.items[1]});
    }
}

UInt128 BackupEntryWithChecksumCalculation::getChecksum(const ReadSettings & read_settings) const
{
    if (!calculated_checksum)
    {
        auto full_checksum = calculateChecksum(getSize(), read_settings);
        chassert(full_checksum);
        calculated_checksum = full_checksum.value();
    }

    return *calculated_checksum;
}


std::optional<UInt128> BackupEntryWithChecksumCalculation::getPartialChecksum(UInt64 limit, const ReadSettings & read_settings) const
{
    UInt64 size = getSize();
    if (limit >= size)
        return getChecksum(read_settings);

    if (calculated_checksum)
        return calculateChecksum(limit, read_settings);

    auto [partial_checksum, full_checksum] = calculateChecksum(limit, size, read_settings);
    chassert(full_checksum);
    calculated_checksum = full_checksum.value();
    return partial_checksum;
}


std::optional<UInt128> BackupEntryWithChecksumCalculation::calculateChecksum(UInt64 limit, const ReadSettings & read_settings) const
{
    return calculateChecksum(limit, {}, read_settings).first;
}


BackupEntryWithChecksumCalculation::ChecksumCalculationMethod BackupEntryWithChecksumCalculation::chooseChecksumCalculationMethod() const
{
    UInt64 size = getSize();

    ChecksumCalculationMethod method;
    if (size == 0)
    {
        method = ChecksumCalculationMethod::EmptyZero;
    }
    else if (hasPrecalculatedChecksum() && !isEncryptedByDisk())
    {
        method = ChecksumCalculationMethod::Precalculated;
    }
    else if (hasPrecalculatedChecksum() && isEncryptedByDisk())
    {
        method = ChecksumCalculationMethod::PrecalculatedCombinedWithEncryptionIV;
    }
    else
    {
        method = ChecksumCalculationMethod::FromReading;
    }

    return method;
}


bool BackupEntryWithChecksumCalculation::hasPrecalculatedChecksum() const
{
    return getPrecalculatedChecksum().has_value();
}


std::pair<std::optional<UInt128>, std::optional<UInt128>> BackupEntryWithChecksumCalculation::calculateChecksum(
    UInt64 limit, std::optional<UInt64> second_limit, const ReadSettings & read_settings) const
{
    switch (chooseChecksumCalculationMethod())
    {
        case ChecksumCalculationMethod::EmptyZero:
            return {0, 0};

        case ChecksumCalculationMethod::Precalculated:
            return getPrecalculatedChecksumIfFull(limit, second_limit);

        case ChecksumCalculationMethod::PrecalculatedCombinedWithEncryptionIV:
            return combinePrecalculatedChecksumWithEncryptionIV(limit, second_limit);

        case ChecksumCalculationMethod::FromReading:
            return calculateChecksumFromReading(limit, second_limit, read_settings);
    }
    UNREACHABLE();
}


std::pair<std::optional<UInt128>, std::optional<UInt128>>
BackupEntryWithChecksumCalculation::getPrecalculatedChecksumIfFull(UInt64 limit, std::optional<UInt64> second_limit) const
{
    UInt64 size = getSize();
    limit = std::min(limit, size);
    if (second_limit)
        second_limit = std::min(*second_limit, size);

    std::optional<UInt128> checksum, second_checksum;
    if (limit == size)
        checksum = getPrecalculatedChecksum().value();
    if (second_limit == size)
        second_checksum = getPrecalculatedChecksum().value();

    return {checksum, second_checksum};
}


std::pair<std::optional<UInt128>, std::optional<UInt128>>
BackupEntryWithChecksumCalculation::combinePrecalculatedChecksumWithEncryptionIV(UInt64 limit, std::optional<UInt64> second_limit) const
{
    auto [checksum, second_checksum] = getPrecalculatedChecksumIfFull(limit, second_limit);

    UInt128 iv;
    if (checksum || second_checksum)
        iv = getDisk()->getEncryptedFileIV(getFilePath());

    if (checksum)
        checksum = combineChecksums(*checksum, iv);

    if (second_checksum)
        second_checksum = combineChecksums(*second_checksum, iv);

    return {checksum, second_checksum};
}


std::pair<std::optional<UInt128>, std::optional<UInt128>> BackupEntryWithChecksumCalculation::calculateChecksumFromReading(
    UInt64 limit, std::optional<UInt64> second_limit, const ReadSettings & read_settings) const
{
    UInt64 size = getSize();
    limit = std::min(limit, size);
    if (second_limit)
        second_limit = std::min(*second_limit, size);

    if ((limit == 0) && (second_limit.value_or(0) == 0))
        return {0, 0};

    UInt64 read_size = second_limit.value_or(limit);

    auto read_buffer = getReadBuffer(read_settings.adjustBufferSize(read_size));
    HashingReadBuffer hashing_read_buffer{*read_buffer};
    UInt64 current_offset = 0;

    auto calculate_hash = [&](UInt64 limit_, UInt128 previous_hash) -> UInt128
    {
        if (limit_ == current_offset)
            return previous_hash;
        chassert(limit_ > current_offset);
        current_offset += hashing_read_buffer.tryIgnore(limit_ - current_offset);
        if (current_offset == limit_)
            return hashing_read_buffer.getHash();
        throw Exception(ErrorCodes::UNEXPECTED_END_OF_FILE,
                        "Size of file {} decreased ({} -> {}) unexpectedly while making a backup",
                        getFilePath(), size, current_offset);
    };

    std::optional<UInt128> checksum = calculate_hash(limit, 0);
    std::optional<UInt128> second_checksum;
    if (second_limit)
        second_checksum = calculate_hash(*second_limit, *checksum);

    return {checksum, second_checksum};
}

}
