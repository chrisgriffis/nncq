#pragma once
#include <string>

#include "orbitkit/core/Clock.h"
#include "orbitkit/sched/Ephemeris.h"

namespace orbitkit::sched {

enum class PassPhase { Upcoming, Acquiring, Tracking, Fading, Done };

/// One visibility window of one satellite over the station: acquisition of signal (AOS) to loss (LOS).
class Pass {
public:
    Pass(std::string satellite, core::Clock::Millis aos, core::Clock::Millis los, double maxElevationDeg,
         double downlinkHz, const Ephemeris* ephemeris);

    /// Moves the phase forward for the given time and returns it. Acquiring and Fading are the first and
    /// last 30 seconds, when the signal is usually too weak to decode.
    PassPhase advance(core::Clock::Millis now);

    bool contains(core::Clock::Millis t) const { return t >= aos_ && t <= los_; }
    core::Clock::Millis duration() const { return los_ - aos_; }

    const std::string& satellite() const { return satellite_; }
    core::Clock::Millis aos() const { return aos_; }
    core::Clock::Millis los() const { return los_; }
    double maxElevation() const { return maxElevationDeg_; }
    double downlinkHz() const { return downlinkHz_; }
    PassPhase phase() const { return phase_; }
    const Ephemeris* ephemeris() const { return ephemeris_; }

private:
    std::string satellite_;
    core::Clock::Millis aos_;
    core::Clock::Millis los_;
    double maxElevationDeg_;
    double downlinkHz_;
    const Ephemeris* ephemeris_;
    PassPhase phase_ = PassPhase::Upcoming;
};

const char* phaseName(PassPhase phase);

}  // namespace orbitkit::sched
