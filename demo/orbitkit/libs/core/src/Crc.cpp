#include "orbitkit/core/Crc.h"

#include <array>

namespace orbitkit::core {

const std::uint16_t* Crc::table16()
{
    static const std::array<std::uint16_t, 256> table = [] {
        std::array<std::uint16_t, 256> t{};
        for (unsigned i = 0; i < 256; ++i) {
            std::uint16_t c = static_cast<std::uint16_t>(i << 8);
            for (int b = 0; b < 8; ++b)
                c = (c & 0x8000) ? static_cast<std::uint16_t>((c << 1) ^ 0x1021) : static_cast<std::uint16_t>(c << 1);
            t[i] = c;
        }
        return t;
    }();
    return table.data();
}

std::uint16_t Crc::ccitt16(const std::uint8_t* data, std::size_t size, std::uint16_t seed)
{
    const std::uint16_t* t = table16();
    std::uint16_t crc = seed;
    for (std::size_t i = 0; i < size; ++i)
        crc = static_cast<std::uint16_t>((crc << 8) ^ t[((crc >> 8) ^ data[i]) & 0xFF]);
    return crc;
}

std::uint32_t Crc::crc32(const std::uint8_t* data, std::size_t size, std::uint32_t seed)
{
    std::uint32_t crc = ~seed;
    for (std::size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (int b = 0; b < 8; ++b)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

bool Crc::check(const std::uint8_t* frame, std::size_t size)
{
    if (size < 3)
        return false;
    const std::uint16_t expected = static_cast<std::uint16_t>((frame[size - 2] << 8) | frame[size - 1]);
    return ccitt16(frame, size - 2) == expected;
}

}  // namespace orbitkit::core
