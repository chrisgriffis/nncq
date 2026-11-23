#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

#include "orbitkit/core/Clock.h"

namespace orbitkit::core {

using RawBuffer = std::vector<std::uint8_t>;

enum class FrameKind : std::uint8_t {
    Unknown,
    Telemetry,     // housekeeping and payload data
    Telecommand,   // echo of an uplinked command
    Idle,          // fill frame, carries no packets
    Beacon,        // short plain-text status, sent without a schedule
};

/// One transfer frame as received from the radio, after sync and CRC.
struct Frame {
    struct Header {
        std::uint16_t spacecraftId = 0;
        std::uint8_t virtualChannel = 0;
        std::uint8_t sequence = 0;
        FrameKind kind = FrameKind::Unknown;
    };

    Header header;
    RawBuffer payload;
    Clock::Millis receivedAt = 0;
    float snrDb = 0.0f;

    bool empty() const { return payload.empty(); }
    std::size_t size() const;

    static const char* kindName(FrameKind kind);
};

}  // namespace orbitkit::core
