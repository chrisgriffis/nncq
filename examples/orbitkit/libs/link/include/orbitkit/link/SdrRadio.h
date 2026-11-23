#pragma once
#include <cstdint>
#include <string>

#include "orbitkit/core/RingBuffer.h"
#include "orbitkit/link/Radio.h"

namespace orbitkit::link {

/// A software-defined radio front end. The driver callback pushes demodulated bytes into a ring buffer;
/// receive() drains it on the pipeline thread.
class SdrRadio : public RadioBase {
public:
    struct Settings {
        std::string device = "sdr0";
        int sampleRate = 2'400'000;
        float gainDb = 32.0f;
        bool biasTee = false;   // powers the mast pre-amplifier
    };

    explicit SdrRadio(Settings settings);
    ~SdrRadio() override;

    bool tune(double frequencyHz) override;
    std::size_t receive(core::RawBuffer& out, std::size_t maxBytes) override;
    const char* name() const override { return "sdr"; }

    /// Called from the driver thread with freshly demodulated bytes.
    void onSamples(const std::uint8_t* bytes, std::size_t count);

private:
    bool open();
    void close();

    Settings settings_;
    core::RingBuffer<std::uint8_t, 65536> rx_;
    void* handle_ = nullptr;
};

}  // namespace orbitkit::link
