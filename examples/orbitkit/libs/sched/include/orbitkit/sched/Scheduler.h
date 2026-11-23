#pragma once
#include <cstddef>
#include <vector>

#include "orbitkit/core/Clock.h"
#include "orbitkit/link/Antenna.h"
#include "orbitkit/link/Radio.h"
#include "orbitkit/sched/Pass.h"
#include "orbitkit/sched/Tracker.h"
#include "orbitkit/telemetry/Pipeline.h"

namespace orbitkit::sched {

/// Decides which pass the station works right now. Each tick it picks the best pass in view, keeps the
/// tracker pointing at it, retunes the radio for Doppler and runs the telemetry pipeline.
class Scheduler {
public:
    struct Slot {
        Pass pass;
        int priority = 0;        // higher wins when passes overlap
        bool claimed = false;    // worked at least once
    };

    Scheduler(core::Clock& clock, telemetry::Pipeline& pipeline, link::IRadio& radio, link::Antenna& antenna,
              GroundSite site);

    void plan(std::vector<Pass> passes, int priority = 0);
    void tick(core::Clock::Millis now);

    const Slot* active() const { return active_; }
    std::size_t pending() const;

    /// Called back by the tracker when the antenna lost the satellite for too long.
    void onTrackerLost(const Pass& pass);

private:
    Slot* pickNext(core::Clock::Millis now);
    void retune(core::Clock::Millis now);

    core::Clock& clock_;
    telemetry::Pipeline& pipeline_;
    link::IRadio& radio_;
    GroundSite site_;
    Tracker tracker_;
    std::vector<Slot> slots_;
    Slot* active_ = nullptr;
    std::size_t framesPerTick_ = 32;
};

}  // namespace orbitkit::sched
