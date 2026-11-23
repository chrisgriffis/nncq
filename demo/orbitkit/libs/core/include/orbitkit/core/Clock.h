#pragma once
#include <cstdint>

namespace orbitkit::core {

/// Mission clock in UTC milliseconds since the Unix epoch. It can be frozen at a fixed time, which is how
/// recorded passes are replayed and how the unit tests get deterministic timestamps.
class Clock {
public:
    using Millis = std::int64_t;

    Clock() = default;
    explicit Clock(Millis offset);

    /// Current time: the system clock plus the offset, or the frozen instant.
    Millis now() const;

    void freeze(Millis at);
    void thaw();
    bool frozen() const { return frozen_; }

    /// Parses "2026-03-14T09:26:53Z" (seconds precision, UTC only). Returns -1 on malformed input.
    static Millis fromIso8601(const char* text);

private:
    Millis offset_ = 0;
    Millis frozenAt_ = 0;
    bool frozen_ = false;
};

}  // namespace orbitkit::core
