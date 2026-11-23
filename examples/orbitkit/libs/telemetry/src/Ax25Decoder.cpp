#include "orbitkit/telemetry/Ax25Decoder.h"

#include <utility>

namespace orbitkit::telemetry {

namespace {
constexpr std::uint32_t kFlags = 0x7E7E7E7E;
constexpr std::size_t kAddressBytes = 14;
}  // namespace

Ax25Decoder::Ax25Decoder(std::string expectedCallsign)
    : FramedDecoder(kFlags, 256), callsign_(std::move(expectedCallsign))
{
}

std::string Ax25Decoder::readCallsign(const std::uint8_t* field)
{
    std::string out;
    for (int i = 0; i < 6; ++i) {
        const char c = static_cast<char>(field[i] >> 1);
        if (c != ' ')
            out.push_back(c);
    }
    return out;
}

DecodeResult Ax25Decoder::decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame)
{
    DecodeResult result;
    if (size < kAddressBytes + 2) {
        result.status = DecodeStatus::Truncated;
        return result;
    }
    if (!callsign_.empty() && readCallsign(body + 7) != callsign_) {
        result.status = DecodeStatus::Unsupported;   // somebody else's satellite
        return result;
    }
    frame.header.kind = core::FrameKind::Telemetry;
    frame.payload.assign(body + kAddressBytes + 2, body + size);
    Packet p;
    p.apid = 0x0A0;
    p.data = frame.payload;
    result.packets.push_back(std::move(p));
    return result;
}

DecodeResult BeaconDecoder::decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame)
{
    DecodeResult result = Ax25Decoder::decodeBody(body, size, frame);
    if (!result.ok())
        return result;
    frame.header.kind = core::FrameKind::Beacon;
    for (Packet& p : result.packets)
        p.apid = 0x100;
    return result;
}

}  // namespace orbitkit::telemetry
