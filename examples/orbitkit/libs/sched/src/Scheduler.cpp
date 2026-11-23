#include "orbitkit/sched/Scheduler.h"

#include <algorithm>
#include <utility>

#include "orbitkit/core/Log.h"
#include "orbitkit/link/SdrRadio.h"

namespace orbitkit::sched {

Scheduler::Scheduler(core::Clock& clock, telemetry::Pipeline& pipeline, link::IRadio& radio, link::Antenna& antenna,
                     GroundSite site)
    : clock_(clock), pipeline_(pipeline), radio_(radio), site_(site), tracker_(*this, antenna, site)
{
}

void Scheduler::plan(std::vector<Pass> passes, int priority)
{
    for (Pass& p : passes)
        slots_.push_back(Slot{std::move(p), priority, false});
    std::sort(slots_.begin(), slots_.end(), [](const Slot& a, const Slot& b) { return a.pass.aos() < b.pass.aos(); });
    active_ = nullptr;   // pointers into slots_ are invalid after the sort
}

std::size_t Scheduler::pending() const
{
    return static_cast<std::size_t>(std::count_if(slots_.begin(), slots_.end(),
                                                  [](const Slot& s) { return s.pass.phase() != PassPhase::Done; }));
}

Scheduler::Slot* Scheduler::pickNext(core::Clock::Millis now)
{
    Slot* best = nullptr;
    for (Slot& s : slots_) {
        if (!s.pass.contains(now))
            continue;
        if (!best || s.priority > best->priority
            || (s.priority == best->priority && s.pass.maxElevation() > best->pass.maxElevation()))
            best = &s;
    }
    return best;
}

void Scheduler::retune(core::Clock::Millis now)
{
    const Pass& pass = active_->pass;
    if (radio_.state() == link::LinkState::Idle)
        radio_.tune(pass.downlinkHz());
    if (auto* base = dynamic_cast<link::RadioBase*>(&radio_); base && pass.ephemeris())
        base->applyDoppler(pass.ephemeris()->dopplerHz(site_, now, pass.downlinkHz()));
}

void Scheduler::tick(core::Clock::Millis now)
{
    Slot* next = pickNext(now);
    if (next != active_) {
        if (active_)
            pipeline_.reset();
        active_ = next;
        if (active_) {
            core::logInfo("sched", "working " + active_->pass.satellite());
            tracker_.follow(active_->pass);
        }
    }
    if (!active_)
        return;
    active_->claimed = true;
    tracker_.update(now);
    retune(now);
    if (active_->pass.advance(now) == PassPhase::Tracking && tracker_.locked())
        pipeline_.run(framesPerTick_);
}

void Scheduler::onTrackerLost(const Pass& pass)
{
    core::logWarn("sched", "tracker lost " + pass.satellite() + ", skipping the rest of the pass");
    for (Slot& s : slots_)
        if (&s.pass == &pass)
            s.priority = -1;
    active_ = nullptr;
}

}  // namespace orbitkit::sched
