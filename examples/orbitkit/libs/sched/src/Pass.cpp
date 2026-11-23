#include "orbitkit/sched/Pass.h"

#include <utility>

namespace orbitkit::sched {

namespace {
constexpr core::Clock::Millis kEdgeMs = 30'000;
}

Pass::Pass(std::string satellite, core::Clock::Millis aos, core::Clock::Millis los, double maxElevationDeg,
           double downlinkHz, const Ephemeris* ephemeris)
    : satellite_(std::move(satellite)), aos_(aos), los_(los), maxElevationDeg_(maxElevationDeg),
      downlinkHz_(downlinkHz), ephemeris_(ephemeris)
{
}

PassPhase Pass::advance(core::Clock::Millis now)
{
    if (now < aos_)
        phase_ = PassPhase::Upcoming;
    else if (now > los_)
        phase_ = PassPhase::Done;
    else if (now < aos_ + kEdgeMs)
        phase_ = PassPhase::Acquiring;
    else if (now > los_ - kEdgeMs)
        phase_ = PassPhase::Fading;
    else
        phase_ = PassPhase::Tracking;
    return phase_;
}

const char* phaseName(PassPhase phase)
{
    switch (phase) {
    case PassPhase::Upcoming: return "upcoming";
    case PassPhase::Acquiring: return "acquiring";
    case PassPhase::Tracking: return "tracking";
    case PassPhase::Fading: return "fading";
    default: return "done";
    }
}

}  // namespace orbitkit::sched
