#pragma once
#include <string>

namespace orbitkit::core {

enum class Level { Debug, Info, Warn, Error };

/// Minimal thread-safe logger: one line per message, "<time> <level> <tag>: <text>", to stderr.
class Log {
public:
    static void write(Level level, const char* tag, const std::string& text);
    static void setLevel(Level level);
    static Level level();
};

void logInfo(const char* tag, const std::string& text);
void logWarn(const char* tag, const std::string& text);

}  // namespace orbitkit::core
