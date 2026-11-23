#include "orbitkit/telemetry/Decoder.h"

#include "orbitkit/core/Crc.h"

namespace orbitkit::telemetry {

FramedDecoder::FramedDecoder(std::uint32_t syncMarker, std::size_t frameSize)
    : syncMarker_(syncMarker), frameSize_(frameSize)
{
}

std::size_t FramedDecoder::findSync(const core::RawBuffer& raw) const
{
    std::uint32_t window = 0;
    for (std::size_t i = 0; i < raw.size(); ++i) {
        window = (window << 8) | raw[i];
        if (i >= 3 && window == syncMarker_)
            return i + 1;   // first byte after the marker
    }
    return raw.size();
}

DecodeResult FramedDecoder::decode(const core::RawBuffer& raw, core::Frame& frame)
{
    DecodeResult result;
    const std::size_t start = findSync(raw);
    if (start >= raw.size()) {
        result.status = DecodeStatus::NoSync;
        return result;
    }
    if (raw.size() - start < frameSize_) {
        result.status = DecodeStatus::Truncated;
        return result;
    }
    const std::uint8_t* body = raw.data() + start;
    if (!core::Crc::check(body, frameSize_)) {
        ++crcFailures_;
        result.status = DecodeStatus::BadCrc;   // corrupted on the air: drop the whole frame
        return result;
    }
    return decodeBody(body, frameSize_ - 2, frame);
}

}  // namespace orbitkit::telemetry
