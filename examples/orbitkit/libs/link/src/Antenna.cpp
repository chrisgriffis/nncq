#include "orbitkit/link/Antenna.h"

#include <algorithm>
#include <cmath>

namespace orbitkit::link {

namespace {
double approach(double from, double to, double maxStep)
{
    return from + std::clamp(to - from, -maxStep, maxStep);
}
}  // namespace

Antenna::Antenna(double maxRateDegPerS) : maxRateDegPerS_(maxRateDegPerS) {}

void Antenna::point(const Pointing& target)
{
    target_.azimuthDeg = std::fmod(target.azimuthDeg + 360.0, 360.0);
    target_.elevationDeg = std::clamp(target.elevationDeg, 0.0, 90.0);
}

void Antenna::step(double dtSeconds)
{
    const double maxStep = maxRateDegPerS_ * dtSeconds;
    current_.azimuthDeg = approach(current_.azimuthDeg, target_.azimuthDeg, maxStep);
    current_.elevationDeg = approach(current_.elevationDeg, target_.elevationDeg, maxStep);
}

bool Antenna::slewing() const
{
    return std::abs(current_.azimuthDeg - target_.azimuthDeg) > 0.5
        || std::abs(current_.elevationDeg - target_.elevationDeg) > 0.5;
}

void Antenna::park()
{
    point({0.0, 90.0});
}

}  // namespace orbitkit::link
