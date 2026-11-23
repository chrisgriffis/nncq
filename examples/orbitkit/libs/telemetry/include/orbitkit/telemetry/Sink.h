#pragma once
#include <cstdint>
#include <fstream>
#include <string>

#include "orbitkit/telemetry/Packet.h"

namespace orbitkit::telemetry {

/// Where decoded packets go. The pipeline fans every packet out to all of its sinks.
class ISink {
public:
    virtual ~ISink() = default;
    virtual void write(const Packet& packet) = 0;
    virtual void flush() {}
};

/// Appends packets to a per-pass archive file, "<dir>/<satellite>-<aos>.okp", with a CRC-32 trailer on close.
class FileSink : public ISink {
public:
    explicit FileSink(std::string path);
    ~FileSink() override;

    void write(const Packet& packet) override;
    void flush() override;

private:
    std::string path_;
    std::ofstream out_;
    std::uint32_t crc_ = 0;
};

/// Forwards each packet as one UDP datagram to the mission-control software.
class UdpSink : public ISink {
public:
    UdpSink(std::string host, std::uint16_t port);
    void write(const Packet& packet) override;

private:
    std::string host_;
    std::uint16_t port_;
    std::uint64_t sent_ = 0;
};

/// One summary line per packet on stdout, for operators watching a pass live.
class ConsoleSink : public ISink {
public:
    void write(const Packet& packet) override;
};

}  // namespace orbitkit::telemetry
