#include "orbitkit/link/LoopbackRadio.h"

#include <fstream>
#include <utility>

namespace orbitkit::link {

namespace {
int hexValue(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
}  // namespace

bool LoopbackRadio::load(const std::string& path)
{
    std::ifstream in(path);
    if (!in)
        return false;
    std::string line;
    while (std::getline(in, line)) {
        core::RawBuffer frame;
        for (std::size_t i = 0; i + 1 < line.size(); i += 2) {
            const int hi = hexValue(line[i]), lo = hexValue(line[i + 1]);
            if (hi < 0 || lo < 0)
                break;
            frame.push_back(static_cast<std::uint8_t>(hi << 4 | lo));
        }
        if (!frame.empty())
            inject(std::move(frame));
    }
    setState(LinkState::Locked);
    return true;
}

void LoopbackRadio::inject(core::RawBuffer frame)
{
    recorded_.push_back(std::move(frame));
}

std::size_t LoopbackRadio::receive(core::RawBuffer& out, std::size_t maxBytes)
{
    if (cursor_ >= recorded_.size()) {
        setState(LinkState::Lost);
        return 0;
    }
    const core::RawBuffer& next = recorded_[cursor_];
    if (next.size() > maxBytes)
        return 0;
    out.insert(out.end(), next.begin(), next.end());
    ++cursor_;
    return next.size();
}

}  // namespace orbitkit::link
