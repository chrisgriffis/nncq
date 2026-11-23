add_executable(groundd
  main.cpp
  Station.cpp
)
target_include_directories(groundd PRIVATE .)
target_link_libraries(groundd PRIVATE orbit_sched orbit_telemetry orbit_link orbit_core)
