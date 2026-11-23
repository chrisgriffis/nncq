#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

#include "orbitkit/core/Frame.h"
#include "orbitkit/telemetry/Packet.h"

namespace orbitkit::telemetry {

enum class DecodeStatus { Ok, NoSync, BadCrc, Truncated, Unsupported };

struct DecodeResult {
    DecodeStatus status = DecodeStatus::Ok;
    std::vector<Packet> packets;

    bool ok() const { return status == DecodeStatus::Ok; }
};

/// Turns raw bytes from a radio into a frame and its packets.
class IDecoder {
public:
    virtual ~IDecoder() = default;

    virtual DecodeResult decode(const core::RawBuffer& raw, core::Frame& frame) = 0;
    virtual const char* protocol() const = 0;
};

/// Finds the attached sync marker, checks the frame's CRC-16 and hands the body to decodeBody().
/// Frames that fail the CRC check are counted and dropped here, before any protocol code sees them.
class FramedDecoder : public IDecoder {
public:
    FramedDecoder(std::uint32_t syncMarker, std::size_t frameSize);

    DecodeResult decode(const core::RawBuffer& raw, core::Frame& frame) override;

    std::uint64_t crcFailures() const { return crcFailures_; }

protected:
    virtual DecodeResult decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame) = 0;

    std::size_t findSync(const core::RawBuffer& raw) const;

    std::uint32_t syncMarker_;
    std::size_t frameSize_;
    std::uint64_t crcFailures_ = 0;
};

}  // namespace orbitkit::telemetry
