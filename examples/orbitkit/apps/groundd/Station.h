#pragma once
#include <memory>
#include <string>
#include <vector>

#include "orbitkit/core/Clock.h"
#include "orbitkit/link/Antenna.h"
#include "orbitkit/link/Radio.h"
#include "orbitkit/sched/Ephemeris.h"
#include "orbitkit/sched/Scheduler.h"
#include "orbitkit/telemetry/Pipeline.h"

namespace orbitkit::app {

struct StationConfig {
    std::string name = "station";
    sched::GroundSite site;
    std::string radio = "sdr";             // sdr | loopback
    std::string replayFile;                // loopback: recorded pass to replay
    std::string protocol = "ccsds";        // see telemetry::makeDecoder
    std::string archiveDir = "archive";
    std::uint16_t udpPort = 0;             // 0 = no UDP forwarding
    bool console = true;
};

/// The ground-station daemon: owns the clock, antenna, radio, pipeline and scheduler and ticks them.
class Station {
public:
    explicit Station(StationConfig config);
    ~Station();

    /// Loads the satellites' TLEs and plans the passes of the next day.
    bool prepare(const std::string& tleFile);
    /// Ticks the scheduler once per second until stop() or maxTicks (0 = forever). Returns an exit code.
    int run(int maxTicks);
    void stop() { running_ = false; }

    static StationConfig loadConfig(const std::string& path);

private:
    std::unique_ptr<link::IRadio> makeRadio() const;
    std::vector<sched::Pass> predictPasses(const sched::Ephemeris& sat, core::Clock::Millis from) const;

    StationConfig config_;
    core::Clock clock_;
    link::Antenna antenna_;
    std::vector<sched::Ephemeris> satellites_;
    std::unique_ptr<link::IRadio> radio_;
    std::unique_ptr<telemetry::Pipeline> pipeline_;
    std::unique_ptr<sched::Scheduler> scheduler_;
    bool running_ = false;
};

}  // namespace orbitkit::app
