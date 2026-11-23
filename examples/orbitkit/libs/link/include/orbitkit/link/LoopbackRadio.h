#pragma once
#include <string>
#include <vector>

#include "orbitkit/link/Radio.h"

namespace orbitkit::link {

/// Replays a recorded pass (one frame per line, hex encoded) as if it came off the air. Used by the tests
/// and by `groundd --replay`.
class LoopbackRadio : public RadioBase {
public:
    LoopbackRadio() = default;

    bool load(const std::string& path);
    void inject(core::RawBuffer frame);

    std::size_t receive(core::RawBuffer& out, std::size_t maxBytes) override;
    const char* name() const override { return "loopback"; }

    std::size_t remaining() const { return recorded_.size() - cursor_; }

private:
    std::vector<core::RawBuffer> recorded_;
    std::size_t cursor_ = 0;
};

}  // namespace orbitkit::link
