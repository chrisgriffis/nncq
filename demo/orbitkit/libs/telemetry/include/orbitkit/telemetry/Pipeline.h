#pragma once
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "orbitkit/core/Clock.h"
#include "orbitkit/link/Radio.h"
#include "orbitkit/telemetry/Decoder.h"
#include "orbitkit/telemetry/Sink.h"

namespace orbitkit::telemetry {

/// Radio -> decoder -> sinks. One pipeline per station; the scheduler runs it every tick while a pass is
/// being tracked.
class Pipeline {
public:
    struct Stats {
        std::uint64_t frames = 0;
        std::uint64_t packets = 0;
        std::uint64_t crcErrors = 0;
        std::uint64_t dropped = 0;
        double meanSnrDb = 0.0;
    };

    Pipeline(link::IRadio& radio, std::unique_ptr<IDecoder> decoder, core::Clock& clock);

    void addSink(std::shared_ptr<ISink> sink);

    /// Receives and decodes up to frameBudget frames; returns the number of packets dispatched.
    std::size_t run(std::size_t frameBudget);

    const Stats& stats() const { return stats_; }
    const IDecoder& decoder() const { return *decoder_; }
    void reset();

private:
    void dispatch(const Packet& packet);
    void account(const DecodeResult& result, const core::Frame& frame);

    link::IRadio& radio_;
    std::unique_ptr<IDecoder> decoder_;
    std::vector<std::shared_ptr<ISink>> sinks_;
    core::Clock& clock_;
    core::RawBuffer scratch_;
    Stats stats_;
};

/// "ccsds", "ax25:<callsign>" or "beacon:<callsign>".
std::unique_ptr<IDecoder> makeDecoder(const std::string& protocol);

}  // namespace orbitkit::telemetry
