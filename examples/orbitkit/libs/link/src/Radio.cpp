#include "orbitkit/link/Radio.h"

#include <string>

#include "orbitkit/core/Log.h"

namespace orbitkit::link {

bool RadioBase::tune(double frequencyHz)
{
    if (frequencyHz <= 0.0)
        return false;
    frequencyHz_ = frequencyHz;
    dopplerHz_ = 0.0;
    setState(LinkState::Tuning);
    return true;
}

void RadioBase::applyDoppler(double offsetHz)
{
    dopplerHz_ = offsetHz;
}

void RadioBase::setState(LinkState state)
{
    if (state == state_)
        return;
    if (state == LinkState::Lost)
        core::logWarn(name(), "carrier lost at " + std::to_string(frequency()) + " Hz");
    state_ = state;
}

}  // namespace orbitkit::link
