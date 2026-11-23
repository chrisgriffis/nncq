#pragma once
#include <string>

#include "orbitkit/core/Clock.h"

namespace orbitkit::sched {

struct GroundSite {
    double latitudeDeg = 0.0;
    double longitudeDeg = 0.0;
    double altitudeM = 0.0;
};

/// Where the satellite appears from a ground site at one instant.
struct Look {
    double azimuthDeg = 0.0;
    double elevationDeg = 0.0;
    double rangeKm = 0.0;
    double rangeRateKmS = 0.0;   // positive = moving away; drives the Doppler correction
};

/// Mean orbital elements from a two-line element set, propagated with a simple two-body model.
/// Good to a few kilometres over a day, which is plenty for pointing a 30-degree beam.
class Ephemeris {
public:
    struct Elements {
        double inclinationDeg = 0.0;
        double raanDeg = 0.0;
        double eccentricity = 0.0;
        double argPerigeeDeg = 0.0;
        double meanAnomalyDeg = 0.0;
        double meanMotionRevPerDay = 0.0;
        core::Clock::Millis epoch = 0;
    };

    static Ephemeris fromTle(const std::string& name, const std::string& line1, const std::string& line2);

    Look lookAngle(const GroundSite& site, core::Clock::Millis at) const;
    double dopplerHz(const GroundSite& site, core::Clock::Millis at, double carrierHz) const;

    const std::string& name() const { return name_; }
    const Elements& elements() const { return elements_; }

private:
    std::string name_;
    Elements elements_;
};

}  // namespace orbitkit::sched
