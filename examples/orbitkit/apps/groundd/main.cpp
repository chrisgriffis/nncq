// groundd: the ground-station daemon.
//
//   groundd [--config config/station.yaml] [--tle config/satellites.tle] [--replay pass.hex] [--ticks N]
//
// Plans the next day's passes, then works them: points the antenna, follows Doppler and decodes telemetry.

#include <cstdio>
#include <cstring>
#include <string>

#include "Station.h"
#include "orbitkit/core/Log.h"

using namespace orbitkit;

namespace {
void usage()
{
    std::fprintf(stderr, "usage: groundd [--config FILE] [--tle FILE] [--replay FILE] [--ticks N] [--verbose]\n");
}
}  // namespace

int main(int argc, char** argv)
{
    std::string config = "config/station.yaml", tle = "config/satellites.tle", replay;
    int ticks = 0;
    for (int i = 1; i < argc; ++i) {
        const bool more = i + 1 < argc;
        if (!std::strcmp(argv[i], "--config") && more) config = argv[++i];
        else if (!std::strcmp(argv[i], "--tle") && more) tle = argv[++i];
        else if (!std::strcmp(argv[i], "--replay") && more) replay = argv[++i];
        else if (!std::strcmp(argv[i], "--ticks") && more) ticks = std::atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--verbose")) core::Log::setLevel(core::Level::Debug);
        else {
            usage();
            return 64;
        }
    }

    app::StationConfig cfg = app::Station::loadConfig(config);
    if (!replay.empty()) {
        cfg.radio = "loopback";
        cfg.replayFile = replay;
    }
    app::Station station{cfg};
    if (!station.prepare(tle)) {
        core::logWarn("groundd", "no satellites in " + tle);
        return 1;
    }
    return station.serve(ticks);
}
