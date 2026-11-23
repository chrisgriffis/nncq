#include "orbitkit/telemetry/CcsdsDecoder.h"

namespace orbitkit::telemetry {

CcsdsDecoder::CcsdsDecoder(std::size_t frameSize) : FramedDecoder(kAsm, frameSize) {}

core::Frame::Header CcsdsDecoder::parseHeader(const std::uint8_t* body)
{
    core::Frame::Header h;
    h.spacecraftId = static_cast<std::uint16_t>(((body[0] & 0x3F) << 4) | (body[1] >> 4));
    h.virtualChannel = static_cast<std::uint8_t>((body[1] >> 1) & 0x07);
    h.sequence = body[2];
    h.kind = h.virtualChannel == 7 ? core::FrameKind::Idle : core::FrameKind::Telemetry;
    return h;
}

std::size_t CcsdsDecoder::extractPackets(const std::uint8_t* data, std::size_t size, DecodeResult& out) const
{
    std::size_t at = 0, count = 0;
    while (at + 6 <= size) {
        Packet p;
        p.apid = static_cast<std::uint16_t>(((data[at] & 0x07) << 8) | data[at + 1]);
        p.sequenceCount = static_cast<std::uint16_t>(((data[at + 2] & 0x3F) << 8) | data[at + 3]);
        const std::size_t length = static_cast<std::size_t>((data[at + 4] << 8) | data[at + 5]) + 1;
        if (at + 6 + length > size)
            break;   // continues in the next frame; not reassembled in this version
        if (!p.idle()) {
            p.body.assign(data + at + 6, data + at + 6 + length);
            out.packets.push_back(std::move(p));
            ++count;
        }
        at += 6 + length;
    }
    return count;
}

DecodeResult CcsdsDecoder::decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame)
{
    DecodeResult result;
    if (size < 6) {
        result.status = DecodeStatus::Truncated;
        return result;
    }
    frame.header = parseHeader(body);
    if (frame.header.sequence != static_cast<std::uint8_t>(lastSequence_ + 1))
        ++sequenceGaps_;
    lastSequence_ = frame.header.sequence;
    frame.payload.assign(body + 6, body + size);
    if (frame.header.kind != core::FrameKind::Idle)
        extractPackets(frame.payload.data(), frame.payload.size(), result);
    return result;
}

}  // namespace orbitkit::telemetry
