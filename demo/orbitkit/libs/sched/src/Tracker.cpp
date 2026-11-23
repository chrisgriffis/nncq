#include "orbitkit/sched/Tracker.h"

#include <cmath>

namespace orbitkit::sched {

namespace {
constexpr int kMaxMisses = 5;
constexpr double kBeamHalfWidthDeg = 15.0;
}  // namespace

Tracker::Tracker(Scheduler& owner, link::Antenna& antenna, GroundSite site)
    : owner_(owner), antenna_(antenna), site_(site)
{
}

void Tracker::follow(const Pass& pass)
{
    pass_ = &pass;
    misses_ = 0;
    lastUpdate_ = 0;
}

void Tracker::update(core::Clock::Millis now)
{
    if (!pass_ || !pass_->ephemeris())
        return;
    const Look look = pass_->ephemeris()->lookAngle(site_, now);
    antenna_.point({look.azimuthDeg, look.elevationDeg});
    if (lastUpdate_ != 0)
        antenna_.step(static_cast<double>(now - lastUpdate_) / 1000.0);
    lastUpdate_ = now;

    const link::Antenna::Pointing at = antenna_.current();
    const bool inBeam = std::abs(at.azimuthDeg - look.azimuthDeg) < kBeamHalfWidthDeg
                     && std::abs(at.elevationDeg - look.elevationDeg) < kBeamHalfWidthDeg;
    misses_ = inBeam ? 0 : misses_ + 1;
    if (misses_ > kMaxMisses) {
        const Pass& lost = *pass_;
        pass_ = nullptr;
        owner_.onTrackerLost(lost);
    }
}

}  // namespace orbitkit::sched
