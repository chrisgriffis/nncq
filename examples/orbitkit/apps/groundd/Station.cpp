#include "Station.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <sstream>
#include <thread>
#include <utility>

#include "orbitkit/core/Log.h"
#include "orbitkit/link/LoopbackRadio.h"
#include "orbitkit/link/SdrRadio.h"

namespace orbitkit::app {

namespace {
constexpr core::Clock::Millis kStepMs = 10'000;
constexpr double kMinElevationDeg = 10.0;
}  // namespace

Station::Station(StationConfig config) : config_(std::move(config))
{
    radio_ = makeRadio();
    pipeline_ = std::make_unique<telemetry::Pipeline>(*radio_, telemetry::makeDecoder(config_.protocol), clock_);
    pipeline_->addSink(std::make_shared<telemetry::FileSink>(config_.archiveDir + "/" + config_.name + ".okp"));
    if (config_.udpPort)
        pipeline_->addSink(std::make_shared<telemetry::UdpSink>("127.0.0.1", config_.udpPort));
    if (config_.console)
        pipeline_->addSink(std::make_shared<telemetry::ConsoleSink>());
    scheduler_ = std::make_unique<sched::Scheduler>(clock_, *pipeline_, *radio_, antenna_, config_.site);
}

Station::~Station()
{
    antenna_.park();
}

std::unique_ptr<link::IRadio> Station::makeRadio() const
{
    if (config_.radio == "loopback") {
        auto radio = std::make_unique<link::LoopbackRadio>();
        if (!config_.replayFile.empty() && !radio->load(config_.replayFile))
            core::logWarn("station", "cannot read replay file " + config_.replayFile);
        return radio;
    }
    return std::make_unique<link::SdrRadio>(link::SdrRadio::Settings{});
}

std::vector<sched::Pass> Station::predictPasses(const sched::Ephemeris& sat, core::Clock::Millis from) const
{
    std::vector<sched::Pass> passes;
    core::Clock::Millis aos = 0;
    double maxEl = 0.0;
    for (core::Clock::Millis t = from; t < from + 86'400'000; t += kStepMs) {
        const double el = sat.lookAngle(config_.site, t).elevationDeg;
        if (el >= kMinElevationDeg && aos == 0) {
            aos = t;
            maxEl = el;
        } else if (el >= kMinElevationDeg) {
            maxEl = std::max(maxEl, el);
        } else if (aos != 0) {
            passes.emplace_back(sat.name(), aos, t, maxEl, 437.5e6, &sat);
            aos = 0;
        }
    }
    return passes;
}

bool Station::prepare(const std::string& tleFile)
{
    std::ifstream in(tleFile);
    std::string name, l1, l2;
    while (std::getline(in, name) && std::getline(in, l1) && std::getline(in, l2))
        satellites_.push_back(sched::Ephemeris::fromTle(name, l1, l2));
    for (const sched::Ephemeris& sat : satellites_)
        scheduler_->plan(predictPasses(sat, clock_.now()));
    core::logInfo("station", std::to_string(scheduler_->pending()) + " passes planned");
    return !satellites_.empty();
}

int Station::run(int maxTicks)
{
    running_ = true;
    for (int tick = 0; running_ && (maxTicks == 0 || tick < maxTicks); ++tick) {
        scheduler_->tick(clock_.now());
        if (!clock_.frozen())
            std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    const auto& s = pipeline_->stats();
    core::logInfo("station", "frames=" + std::to_string(s.frames) + " packets=" + std::to_string(s.packets)
                                 + " crc_errors=" + std::to_string(s.crcErrors));
    return s.frames > 0 && s.crcErrors * 2 > s.frames ? 2 : 0;
}

StationConfig Station::loadConfig(const std::string& path)
{
    StationConfig c;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream kv(line);
        std::string key, value;
        if (!std::getline(kv, key, ':') || !std::getline(kv >> std::ws, value))
            continue;
        if (key == "name") c.name = value;
        else if (key == "latitude") c.site.latitudeDeg = std::stod(value);
        else if (key == "longitude") c.site.longitudeDeg = std::stod(value);
        else if (key == "altitude_m") c.site.altitudeM = std::stod(value);
        else if (key == "radio") c.radio = value;
        else if (key == "protocol") c.protocol = value;
        else if (key == "archive_dir") c.archiveDir = value;
        else if (key == "udp_port") c.udpPort = static_cast<std::uint16_t>(std::stoi(value));
    }
    return c;
}

}  // namespace orbitkit::app
