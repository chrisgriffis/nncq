#pragma once
#include "orbitkit/telemetry/Decoder.h"

namespace orbitkit::telemetry {

/// CCSDS TM transfer frames (fixed length, ASM 0x1ACFFC1D) carrying space packets.
class CcsdsDecoder : public FramedDecoder {
public:
    static constexpr std::uint32_t kAsm = 0x1ACFFC1D;

    explicit CcsdsDecoder(std::size_t frameSize = 223);

    const char* protocol() const override { return "ccsds-tm"; }

protected:
    DecodeResult decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame) override;

private:
    static core::Frame::Header parseHeader(const std::uint8_t* body);
    std::size_t extractPackets(const std::uint8_t* data, std::size_t size, DecodeResult& out) const;

    std::uint8_t lastSequence_ = 0;
    std::uint32_t sequenceGaps_ = 0;
};

}  // namespace orbitkit::telemetry
