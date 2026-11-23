#include <memory>

#include "check.h"
#include "orbitkit/link/LoopbackRadio.h"
#include "orbitkit/sched/Scheduler.h"
#include "orbitkit/telemetry/CcsdsDecoder.h"

using namespace orbitkit;

TEST(scheduler_prefers_the_higher_pass_when_two_overlap)
{
    core::Clock clock;
    link::LoopbackRadio radio;
    link::Antenna antenna;
    telemetry::Pipeline pipeline(radio, std::make_unique<telemetry::CcsdsDecoder>(), clock);
    sched::Scheduler scheduler(clock, pipeline, radio, antenna, sched::GroundSite{47.6, 8.5, 420});

    std::vector<sched::Pass> passes;
    passes.emplace_back("KESTREL-1", 0, 600'000, 23.0, 437.5e6, nullptr);
    passes.emplace_back("AURORA-3", 60'000, 500'000, 71.0, 435.9e6, nullptr);
    scheduler.plan(std::move(passes));

    scheduler.tick(120'000);
    CHECK(scheduler.active() != nullptr);
    CHECK(scheduler.active()->pass.satellite() == "AURORA-3");
    CHECK(scheduler.pending() == 2);
}

TEST(pass_phases_follow_the_clock)
{
    sched::Pass pass("PIKA", 100'000, 400'000, 40.0, 145.8e6, nullptr);
    CHECK(pass.advance(50'000) == sched::PassPhase::Upcoming);
    CHECK(pass.advance(110'000) == sched::PassPhase::Acquiring);
    CHECK(pass.advance(200'000) == sched::PassPhase::Tracking);
    CHECK(pass.advance(390'000) == sched::PassPhase::Fading);
    CHECK(pass.advance(500'000) == sched::PassPhase::Done);
}
