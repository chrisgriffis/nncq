#include "orbitkit/telemetry/Pipeline.h"

#include <utility>

#include "orbitkit/core/Log.h"
#include "orbitkit/telemetry/Ax25Decoder.h"
#include "orbitkit/telemetry/CcsdsDecoder.h"

namespace orbitkit::telemetry {

namespace {
constexpr std::size_t kMaxFrameBytes = 1024;
}

Pipeline::Pipeline(link::IRadio& radio, std::unique_ptr<IDecoder> decoder, core::Clock& clock)
    : radio_(radio), decoder_(std::move(decoder)), clock_(clock)
{
    scratch_.reserve(kMaxFrameBytes);
}

void Pipeline::addSink(std::shared_ptr<ISink> sink)
{
    sinks_.push_back(std::move(sink));
}

std::size_t Pipeline::run(std::size_t frameBudget)
{
    std::size_t dispatched = 0;
    for (std::size_t i = 0; i < frameBudget; ++i) {
        scratch_.clear();
        if (radio_.receive(scratch_, kMaxFrameBytes) == 0)
            break;
        core::Frame frame;
        frame.receivedAt = clock_.now();
        DecodeResult result = decoder_->decode(scratch_, frame);
        account(result, frame);
        if (!result.ok())
            continue;
        for (Packet& packet : result.packets) {
            packet.time = frame.receivedAt;
            packet.source = &frame;
            dispatch(packet);
            ++dispatched;
        }
    }
    return dispatched;
}

void Pipeline::dispatch(const Packet& packet)
{
    for (const auto& sink : sinks_)
        sink->write(packet);
}

void Pipeline::account(const DecodeResult& result, const core::Frame& frame)
{
    ++stats_.frames;
    if (result.status == DecodeStatus::BadCrc)
        ++stats_.crcErrors;
    else if (!result.ok())
        ++stats_.dropped;
    stats_.packets += result.packets.size();
    stats_.meanSnrDb += (frame.snrDb - stats_.meanSnrDb) / static_cast<double>(stats_.frames);
}

void Pipeline::reset()
{
    for (const auto& sink : sinks_)
        sink->flush();
    stats_ = Stats{};
}

std::unique_ptr<IDecoder> makeDecoder(const std::string& protocol)
{
    const auto colon = protocol.find(':');
    const std::string kind = protocol.substr(0, colon);
    const std::string arg = colon == std::string::npos ? "" : protocol.substr(colon + 1);
    if (kind == "ccsds")
        return std::make_unique<CcsdsDecoder>();
    if (kind == "ax25")
        return std::make_unique<Ax25Decoder>(arg);
    if (kind == "beacon")
        return std::make_unique<BeaconDecoder>(arg);
    core::logWarn("pipeline", "unknown protocol '" + protocol + "', falling back to ccsds");
    return std::make_unique<CcsdsDecoder>();
}

}  // namespace orbitkit::telemetry
