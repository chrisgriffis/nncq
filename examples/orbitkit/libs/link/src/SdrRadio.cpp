#include "orbitkit/link/SdrRadio.h"

#include <utility>

#include "orbitkit/core/Log.h"

namespace orbitkit::link {

SdrRadio::SdrRadio(Settings settings) : settings_(std::move(settings)) {}

SdrRadio::~SdrRadio()
{
    close();
}

bool SdrRadio::open()
{
    if (handle_)
        return true;
    // The vendor driver is optional (ORBITKIT_WITH_SDR); without it the radio never locks.
    core::logInfo("sdr", "opening " + settings_.device);
    handle_ = this;
    return true;
}

void SdrRadio::close()
{
    handle_ = nullptr;
    setState(LinkState::Idle);
}

bool SdrRadio::tune(double frequencyHz)
{
    if (!open() || !RadioBase::tune(frequencyHz))
        return false;
    rx_.clear();
    setState(LinkState::Locked);
    return true;
}

void SdrRadio::onSamples(const std::uint8_t* bytes, std::size_t count)
{
    for (std::size_t i = 0; i < count; ++i)
        rx_.push(bytes[i]);
}

std::size_t SdrRadio::receive(core::RawBuffer& out, std::size_t maxBytes)
{
    std::size_t n = 0;
    std::uint8_t b = 0;
    while (n < maxBytes && rx_.pop(b)) {
        out.push_back(b);
        ++n;
    }
    if (rx_.overruns() > 0)
        setState(LinkState::Lost);
    return n;
}

}  // namespace orbitkit::link
