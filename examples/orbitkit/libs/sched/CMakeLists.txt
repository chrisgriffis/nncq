add_library(orbit_sched STATIC
  src/Ephemeris.cpp
  src/Pass.cpp
  src/Scheduler.cpp
  src/Tracker.cpp
)
add_library(orbitkit::sched ALIAS orbit_sched)
target_include_directories(orbit_sched PUBLIC include)
target_link_libraries(orbit_sched PUBLIC orbit_core orbit_link orbit_telemetry)
