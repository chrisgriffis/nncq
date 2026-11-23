#pragma once
#include <cstddef>

#include "orbitkit/core/Frame.h"

namespace orbitkit::link {

enum class LinkState { Idle, Tuning, Locked, Lost };

/// A receiver that yields raw demodulated bytes. Implementations: SdrRadio (hardware), LoopbackRadio (replay).
class IRadio {
public:
    virtual ~IRadio() = default;

    virtual bool tune(double frequencyHz) = 0;
    /// Appends up to maxBytes demodulated bytes to out; returns how many were appended.
    virtual std::size_t receive(core::RawBuffer& out, std::size_t maxBytes) = 0;
    virtual LinkState state() const = 0;
    virtual const char* name() const = 0;
};

/// Bookkeeping shared by the concrete radios: centre frequency, Doppler correction and link state.
class RadioBase : public IRadio {
public:
    bool tune(double frequencyHz) override;
    LinkState state() const override { return state_; }

    /// Shifts the receive frequency by the Doppler offset the tracker computed for the current pass.
    void applyDoppler(double offsetHz);
    double frequency() const { return frequencyHz_ + dopplerHz_; }

protected:
    void setState(LinkState state);

    double frequencyHz_ = 0.0;
    double dopplerHz_ = 0.0;
    LinkState state_ = LinkState::Idle;
};

}  // namespace orbitkit::link
