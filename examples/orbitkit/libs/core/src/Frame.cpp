#include "orbitkit/core/Frame.h"

namespace orbitkit::core {

std::size_t Frame::size() const
{
    return 6 + payload.size() + 2;   // primary header, data field, CRC-16
}

const char* Frame::kindName(FrameKind kind)
{
    switch (kind) {
    case FrameKind::Telemetry: return "telemetry";
    case FrameKind::Telecommand: return "telecommand";
    case FrameKind::Idle: return "idle";
    case FrameKind::Beacon: return "beacon";
    default: return "unknown";
    }
}

}  // namespace orbitkit::core
