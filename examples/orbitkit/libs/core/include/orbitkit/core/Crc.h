#pragma once
#include <cstddef>
#include <cstdint>

namespace orbitkit::core {

/// Checksums used on the downlink: CRC-16/CCITT-FALSE protects every transfer frame (its last two bytes),
/// CRC-32 protects archived files.
class Crc {
public:
    static std::uint16_t ccitt16(const std::uint8_t* data, std::size_t size, std::uint16_t seed = 0xFFFF);
    static std::uint32_t crc32(const std::uint8_t* data, std::size_t size, std::uint32_t seed = 0);

    /// True when the last two bytes of a frame (big endian) are the CRC-16 of everything before them.
    /// A corrupted frame fails this check and is dropped by the decoder.
    static bool check(const std::uint8_t* frame, std::size_t size);

private:
    static const std::uint16_t* table16();
};

}  // namespace orbitkit::core
