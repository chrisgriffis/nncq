#include <algorithm>
#include <iterator>
#include <memory>

#include "check.h"
#include "orbitkit/core/Crc.h"
#include "orbitkit/link/LoopbackRadio.h"
#include "orbitkit/telemetry/CcsdsDecoder.h"
#include "orbitkit/telemetry/Pipeline.h"

using namespace orbitkit;

namespace {

/// Collects packets in memory instead of writing them anywhere.
class MemorySink : public telemetry::ISink {
public:
    void write(const telemetry::Packet& packet) override { packets.push_back(packet); }
    std::vector<telemetry::Packet> packets;
};

core::RawBuffer makeFrame(std::uint8_t sequence, bool corrupt)
{
    core::RawBuffer raw = {0x1A, 0xCF, 0xFC, 0x1D};
    core::RawBuffer body(32, 0);
    body[0] = 0x01;
    body[1] = 0x20;
    body[2] = sequence;
    // one space packet (APID 0x042, 4 data bytes), then an idle packet filling the data field
    const std::uint8_t packet[] = {0x00, 0x42, 0xC0, 0x01, 0x00, 0x03, 'p', 'i', 'n', 'g'};
    const std::uint8_t idle[] = {0x07, 0xFF, 0xC0, 0x00, 0x00, 0x07};
    std::copy(std::begin(packet), std::end(packet), body.begin() + 6);
    std::copy(std::begin(idle), std::end(idle), body.begin() + 16);
    const std::uint16_t crc = core::Crc::ccitt16(body.data(), body.size() - 2);
    body[body.size() - 2] = static_cast<std::uint8_t>(crc >> 8);
    body[body.size() - 1] = static_cast<std::uint8_t>(crc);
    if (corrupt)
        body[10] ^= 0xFF;
    raw.insert(raw.end(), body.begin(), body.end());
    return raw;
}

}  // namespace

TEST(pipeline_decodes_good_frames_and_counts_crc_errors)
{
    core::Clock clock;
    clock.freeze(1'000);
    link::LoopbackRadio radio;
    radio.inject(makeFrame(1, false));
    radio.inject(makeFrame(2, true));
    radio.inject(makeFrame(3, false));

    telemetry::Pipeline pipeline(radio, std::make_unique<telemetry::CcsdsDecoder>(32), clock);
    auto sink = std::make_shared<MemorySink>();
    pipeline.addSink(sink);

    CHECK(pipeline.run(10) == 2);
    CHECK(pipeline.stats().frames == 3);
    CHECK(pipeline.stats().crcErrors == 1);
    CHECK(sink->packets.size() == 2);
    CHECK(sink->packets[0].apid == 0x042);
    CHECK(sink->packets[0].time == 1'000);
}

TEST(make_decoder_falls_back_to_ccsds)
{
    CHECK(std::string(telemetry::makeDecoder("beacon:KST1")->protocol()) == "ax25-beacon");
    CHECK(std::string(telemetry::makeDecoder("morse")->protocol()) == "ccsds-tm");
}
