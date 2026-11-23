#pragma once
#include <string>

#include "orbitkit/telemetry/Decoder.h"

namespace orbitkit::telemetry {

/// AX.25 UI frames as sent by amateur-band cubesats (flag-delimited, callsign addressed).
class Ax25Decoder : public FramedDecoder {
public:
    explicit Ax25Decoder(std::string expectedCallsign);

    const char* protocol() const override { return "ax25"; }
    const std::string& callsign() const { return callsign_; }

protected:
    DecodeResult decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame) override;
    static std::string readCallsign(const std::uint8_t* field);

private:
    std::string callsign_;
};

/// Plain-text beacons ("KST1>BCN: batt=7.9V temp=12C mode=SAFE") carried in AX.25 UI frames.
/// Each beacon becomes one packet on APID 0x100 holding the text.
class BeaconDecoder : public Ax25Decoder {
public:
    using Ax25Decoder::Ax25Decoder;

    const char* protocol() const override { return "ax25-beacon"; }

protected:
    DecodeResult decodeBody(const std::uint8_t* body, std::size_t size, core::Frame& frame) override;
};

}  // namespace orbitkit::telemetry
