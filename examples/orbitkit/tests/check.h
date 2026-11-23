#pragma once
// A deliberately tiny test harness: TEST(name) registers a function, CHECK records failures.
#include <cstdio>
#include <functional>
#include <vector>

namespace check {

struct Case {
    const char* name;
    std::function<void()> fn;
};

inline std::vector<Case>& registry()
{
    static std::vector<Case> cases;
    return cases;
}

inline int& failures()
{
    static int n = 0;
    return n;
}

struct Registrar {
    Registrar(const char* name, std::function<void()> fn) { registry().push_back({name, std::move(fn)}); }
};

}  // namespace check

#define TEST(name)                                                   \
    static void name();                                              \
    static check::Registrar name##_registrar(#name, name);           \
    static void name()

#define CHECK(cond)                                                                    \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            ++check::failures();                                                       \
        }                                                                              \
    } while (0)
