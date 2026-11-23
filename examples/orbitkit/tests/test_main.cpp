#include "check.h"

int main()
{
    for (const auto& c : harness::registry()) {
        std::printf("%-40s", c.name);
        const int before = harness::failures();
        c.fn();
        std::printf("%s\n", harness::failures() == before ? "ok" : "FAILED");
    }
    return harness::failures() ? 1 : 0;
}
