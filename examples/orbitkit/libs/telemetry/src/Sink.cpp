#include "orbitkit/telemetry/Sink.h"

#include <cstdio>
#include <utility>

#include "orbitkit/core/Crc.h"
#include "orbitkit/core/Log.h"

namespace orbitkit::telemetry {

FileSink::FileSink(std::string path) : path_(std::move(path)), out_(path_, std::ios::binary | std::ios::app)
{
    if (!out_)
        core::logWarn("sink", "cannot open archive " + path_);
}

FileSink::~FileSink()
{
    flush();
    const std::uint8_t trailer[4] = {static_cast<std::uint8_t>(crc_ >> 24), static_cast<std::uint8_t>(crc_ >> 16),
                                     static_cast<std::uint8_t>(crc_ >> 8), static_cast<std::uint8_t>(crc_)};
    out_.write(reinterpret_cast<const char*>(trailer), sizeof trailer);
}

void FileSink::write(const Packet& packet)
{
    const std::uint8_t head[4] = {static_cast<std::uint8_t>(packet.apid >> 8), static_cast<std::uint8_t>(packet.apid),
                                  static_cast<std::uint8_t>(packet.data.size() >> 8),
                                  static_cast<std::uint8_t>(packet.data.size())};
    out_.write(reinterpret_cast<const char*>(head), sizeof head);
    out_.write(reinterpret_cast<const char*>(packet.data.data()), static_cast<std::streamsize>(packet.data.size()));
    crc_ = core::Crc::crc32(head, sizeof head, crc_);
    crc_ = core::Crc::crc32(packet.data.data(), packet.data.size(), crc_);
}

void FileSink::flush()
{
    out_.flush();
}

UdpSink::UdpSink(std::string host, std::uint16_t port) : host_(std::move(host)), port_(port) {}

void UdpSink::write(const Packet& packet)
{
    // Socket handling lives in the platform layer; here we only count what would be sent.
    (void)packet;
    ++sent_;
}

void ConsoleSink::write(const Packet& packet)
{
    std::printf("apid=0x%03X seq=%5u len=%4zu\n", packet.apid, packet.sequenceCount, packet.data.size());
}

}  // namespace orbitkit::telemetry
