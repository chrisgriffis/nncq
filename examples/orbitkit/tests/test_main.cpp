#include "check.h"

int main()
{
    for (const auto& c : check::registry()) {
        std::printf("%-40s", c.name);
        const int before = check::failures();
        c.fn();
        std::printf("%s\n", check::failures() == before ? "ok" : "FAILED");
    }
    return check::failures() ? 1 : 0;
}
