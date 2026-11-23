#include "orbitkit/sched/Ephemeris.h"

#include <cmath>
#include <cstdlib>

namespace orbitkit::sched {

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr double kEarthRadiusKm = 6378.137;
constexpr double kMu = 398600.4418;              // km^3/s^2
constexpr double kSpeedOfLightKmS = 299792.458;

double rad(double deg) { return deg * kPi / 180.0; }
double deg(double rad) { return rad * 180.0 / kPi; }

double field(const std::string& line, std::size_t from, std::size_t len)
{
    return std::atof(line.substr(from, len).c_str());
}
}  // namespace

Ephemeris Ephemeris::fromTle(const std::string& name, const std::string& line1, const std::string& line2)
{
    Ephemeris e;
    e.name_ = name;
    const double epochYear = field(line1, 18, 2);
    const double epochDay = field(line1, 20, 12);
    const int year = epochYear < 57 ? 2000 + static_cast<int>(epochYear) : 1900 + static_cast<int>(epochYear);
    const std::string jan1 = std::to_string(year) + "-01-01T00:00:00Z";
    e.elements_.epoch = core::Clock::fromIso8601(jan1.c_str()) + static_cast<core::Clock::Millis>((epochDay - 1.0) * 86400000.0);
    e.elements_.inclinationDeg = field(line2, 8, 8);
    e.elements_.raanDeg = field(line2, 17, 8);
    e.elements_.eccentricity = field(line2, 26, 7) * 1e-7;
    e.elements_.argPerigeeDeg = field(line2, 34, 8);
    e.elements_.meanAnomalyDeg = field(line2, 43, 8);
    e.elements_.meanMotionRevPerDay = field(line2, 52, 11);
    return e;
}

Look Ephemeris::lookAngle(const GroundSite& site, core::Clock::Millis at) const
{
    const double n = elements_.meanMotionRevPerDay * 2.0 * kPi / 86400.0;          // rad/s
    const double a = std::cbrt(kMu / (n * n));
    const double dt = static_cast<double>(at - elements_.epoch) / 1000.0;
    const double m = rad(elements_.meanAnomalyDeg) + n * dt;
    const double u = m + rad(elements_.argPerigeeDeg);                              // circular approximation
    const double i = rad(elements_.inclinationDeg);
    const double earthRot = 7.2921159e-5 * dt;
    const double lon = std::atan2(std::cos(i) * std::sin(u), std::cos(u)) + rad(elements_.raanDeg) - earthRot;
    const double lat = std::asin(std::sin(i) * std::sin(u));

    const double dLon = lon - rad(site.longitudeDeg);
    const double cosC = std::sin(rad(site.latitudeDeg)) * std::sin(lat)
                      + std::cos(rad(site.latitudeDeg)) * std::cos(lat) * std::cos(dLon);
    const double c = std::acos(std::fmax(-1.0, std::fmin(1.0, cosC)));
    const double r = a, re = kEarthRadiusKm + site.altitudeM / 1000.0;

    Look look;
    look.rangeKm = std::sqrt(r * r + re * re - 2 * r * re * cosC);
    look.elevationDeg = deg(std::atan2(cosC - re / r, std::sin(c)));
    look.azimuthDeg = std::fmod(deg(std::atan2(std::sin(dLon) * std::cos(lat),
                                               std::cos(rad(site.latitudeDeg)) * std::sin(lat)
                                               - std::sin(rad(site.latitudeDeg)) * std::cos(lat) * std::cos(dLon))) + 360.0, 360.0);
    return look;
}

double Ephemeris::dopplerHz(const GroundSite& site, core::Clock::Millis at, double carrierHz) const
{
    const Look a = lookAngle(site, at), b = lookAngle(site, at + 1000);
    const double rangeRate = b.rangeKm - a.rangeKm;                                 // km/s
    return -carrierHz * rangeRate / kSpeedOfLightKmS;
}

}  // namespace orbitkit::sched
