#pragma once
#include <array>
#include <cstddef>

namespace orbitkit::core {

/// Fixed-capacity FIFO without allocation, used between the radio's sample callback and the decoder.
/// When full, push() drops the oldest element and counts it as an overrun.
template <typename T, std::size_t N>
class RingBuffer {
public:
    static constexpr std::size_t capacity = N;

    void push(const T& value)
    {
        if (count_ == N) {
            tail_ = (tail_ + 1) % N;
            --count_;
            ++overruns_;
        }
        items_[head_] = value;
        head_ = (head_ + 1) % N;
        ++count_;
    }

    bool pop(T& out)
    {
        if (count_ == 0)
            return false;
        out = items_[tail_];
        tail_ = (tail_ + 1) % N;
        --count_;
        return true;
    }

    std::size_t size() const { return count_; }
    bool empty() const { return count_ == 0; }
    bool full() const { return count_ == N; }
    std::size_t overruns() const { return overruns_; }
    void clear() { head_ = tail_ = count_ = 0; }

private:
    std::array<T, N> items_{};
    std::size_t head_ = 0;
    std::size_t tail_ = 0;
    std::size_t count_ = 0;
    std::size_t overruns_ = 0;
};

}  // namespace orbitkit::core
