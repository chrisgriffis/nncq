#pragma once
#include "orbitkit/core/Clock.h"
#include "orbitkit/link/Antenna.h"
#include "orbitkit/sched/Pass.h"
#include "orbitkit/sched/Scheduler.h"   // FIXME: include cycle with Scheduler.h; only the back pointer is needed

namespace orbitkit::sched {

class Scheduler;

/// Keeps the antenna on the satellite of the active pass. When the satellite stays out of the beam for
/// several ticks it reports the pass as lost to its scheduler.
class Tracker {
public:
    Tracker(Scheduler& owner, link::Antenna& antenna, GroundSite site);

    void follow(const Pass& pass);
    void update(core::Clock::Millis now);
    bool locked() const { return pass_ != nullptr && misses_ == 0; }

private:
    Scheduler& owner_;
    link::Antenna& antenna_;
    const Pass* pass_ = nullptr;
    GroundSite site_;
    int misses_ = 0;
    core::Clock::Millis lastUpdate_ = 0;
};

}  // namespace orbitkit::sched
