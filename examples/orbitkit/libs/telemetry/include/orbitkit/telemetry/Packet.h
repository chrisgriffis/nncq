#pragma once
#include <cstdint>
#include <vector>

#include "orbitkit/core/Frame.h"

namespace orbitkit::telemetry {

/// A space packet extracted from a frame's data field. APID 0x7FF is the idle packet and is never emitted.
struct Packet {
    static constexpr std::uint16_t kIdleApid = 0x7FF;

    std::uint16_t apid = 0;
    std::uint16_t sequenceCount = 0;
    core::Clock::Millis time = 0;
    std::vector<std::uint8_t> body;
    const core::Frame* source = nullptr;   // the frame it came from (valid during dispatch only)

    bool idle() const { return apid == kIdleApid; }
};

}  // namespace orbitkit::telemetry
