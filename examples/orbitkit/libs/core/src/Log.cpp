#include "orbitkit/core/Log.h"

#include <atomic>
#include <cstdio>
#include <mutex>

#include "orbitkit/core/Clock.h"

namespace orbitkit::core {

namespace {
std::atomic<Level> g_level{Level::Info};
std::mutex g_mutex;

const char* levelName(Level level)
{
    switch (level) {
    case Level::Debug: return "DEBUG";
    case Level::Info: return "INFO ";
    case Level::Warn: return "WARN ";
    default: return "ERROR";
    }
}
}  // namespace

void Log::write(Level level, const char* tag, const std::string& text)
{
    if (level < g_level.load())
        return;
    static const Clock clock;
    std::lock_guard<std::mutex> lock(g_mutex);
    std::fprintf(stderr, "%lld %s %s: %s\n", static_cast<long long>(clock.now()), levelName(level), tag, text.c_str());
}

void Log::setLevel(Level level)
{
    g_level = level;
}

Level Log::level()
{
    return g_level.load();
}

void logInfo(const char* tag, const std::string& text)
{
    Log::write(Level::Info, tag, text);
}

void logWarn(const char* tag, const std::string& text)
{
    Log::write(Level::Warn, tag, text);
}

}  // namespace orbitkit::core
