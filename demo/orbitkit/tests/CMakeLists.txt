add_executable(orbit_tests
  test_main.cpp
  test_crc.cpp
  test_pipeline.cpp
  test_scheduler.cpp
)
target_include_directories(orbit_tests PRIVATE .)
target_link_libraries(orbit_tests PRIVATE orbit_sched orbit_telemetry orbit_link orbit_core)
add_test(NAME orbit_tests COMMAND orbit_tests)
