#pragma once

namespace orbitkit::link {

/// Az/el rotator driving the station's yagi pair. Slews at a bounded rate; the tracker asks for a new
/// pointing every tick and reads back where the antenna actually is.
class Antenna {
public:
    struct Pointing {
        double azimuthDeg = 0.0;
        double elevationDeg = 0.0;
    };

    explicit Antenna(double maxRateDegPerS = 6.0);

    void point(const Pointing& target);
    void step(double dtSeconds);
    Pointing current() const { return current_; }
    bool slewing() const;
    void park();

private:
    double maxRateDegPerS_;
    Pointing current_;
    Pointing target_;
};

}  // namespace orbitkit::link
