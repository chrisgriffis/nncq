#include "check.h"
#include "orbitkit/core/Crc.h"

using orbitkit::core::Crc;

TEST(ccitt16_matches_the_reference_vector)
{
    const std::uint8_t text[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    CHECK(Crc::ccitt16(text, sizeof text) == 0x29B1);
}

TEST(crc32_matches_the_reference_vector)
{
    const std::uint8_t text[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    CHECK(Crc::crc32(text, sizeof text) == 0xCBF43926u);
}

TEST(check_rejects_a_flipped_bit)
{
    std::uint8_t frame[] = {0x10, 0x20, 0x30, 0x40, 0x00, 0x00};
    const std::uint16_t crc = Crc::ccitt16(frame, 4);
    frame[4] = static_cast<std::uint8_t>(crc >> 8);
    frame[5] = static_cast<std::uint8_t>(crc);
    CHECK(Crc::check(frame, sizeof frame));
    frame[1] ^= 0x04;
    CHECK(!Crc::check(frame, sizeof frame));
}
