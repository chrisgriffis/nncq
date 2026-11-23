#include "orbitkit/core/Clock.h"

#include <chrono>
#include <cstdio>

namespace orbitkit::core {

Clock::Clock(Millis offset) : offset_(offset) {}

Clock::Millis Clock::now() const
{
    if (frozen_)
        return frozenAt_;
    const auto since = std::chrono::system_clock::now().time_since_epoch();
    return std::chrono::duration_cast<std::chrono::milliseconds>(since).count() + offset_;
}

void Clock::freeze(Millis at)
{
    frozenAt_ = at;
    frozen_ = true;
}

void Clock::thaw()
{
    frozen_ = false;
}

Clock::Millis Clock::fromIso8601(const char* text)
{
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (!text || std::sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2dZ", &y, &mo, &d, &h, &mi, &s) != 6)
        return -1;
    // days from civil (proleptic Gregorian), valid for any year the station will see
    y -= mo <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153 * (mo + (mo > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    const long long days = static_cast<long long>(era) * 146097 + doe - 719468;
    return ((days * 24 + h) * 60 + mi) * 60000LL + s * 1000LL;
}

}  // namespace orbitkit::core
