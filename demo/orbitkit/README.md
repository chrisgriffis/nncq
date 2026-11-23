# orbitkit

> A fictional demo codebase bundled with nncq. It is small, plausible and complete enough to exercise every
> nncq feature (search, call graphs, class / component / package / deployment / sequence diagrams, dependency
> cycles, Python and C++ side by side). It is not a real product and is not meant to be built.

orbitkit is a toolkit for a small amateur satellite ground station. During a pass it points the antenna,
follows the Doppler shift, demodulates the downlink, decodes transfer frames into packets and archives them.

```
          +-----------+     +----------+     +-----------+     +-----------------------+
 TLEs --> | Scheduler | --> | Tracker  | --> |  Antenna  |     |  sinks: file, UDP,    |
          |  (tick)   |     +----------+     +-----------+     |  console              |
          |           | --> +----------+     +-----------+     +-----------------------+
          |           |     |  Radio   | --> | Pipeline  | --> decoder --> packets --^
          +-----------+     +----------+     +-----------+
```

| Folder | Target | What |
|---|---|---|
| `libs/core` | `orbit_core` | clock, CRC-16/32, frames, ring buffer, logging |
| `libs/link` | `orbit_link` | radios (SDR, loopback replay) and the antenna rotator |
| `libs/telemetry` | `orbit_telemetry` | decoders (CCSDS, AX.25, beacons), sinks, the pipeline |
| `libs/sched` | `orbit_sched` | ephemeris, passes, tracker, scheduler |
| `apps/groundd` | `groundd` | the station daemon |
| `tests` | `orbit_tests` | unit tests (tiny built-in harness) |
| `python/orbitkit_tools` | `orbit-tools` | offline re-decoding, pass planning, pass reports |

Build (in principle): `cmake --preset release && cmake --build --preset release`.

See [docs/architecture.md](docs/architecture.md) and [docs/frame-format.md](docs/frame-format.md).
