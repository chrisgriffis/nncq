cmake_minimum_required(VERSION 3.20)
project(orbitkit VERSION 0.4.0 LANGUAGES CXX)

# orbitkit: a small ground-station toolkit. It receives telemetry from small satellites during a pass,
# decodes transfer frames into packets and archives them.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CONFIGURATION_TYPES Debug Release)

option(ORBITKIT_WITH_SDR "Build the SDR radio backend (needs a vendor SDR driver)" ON)
option(ORBITKIT_BUILD_TESTS "Build the unit tests" ON)

add_subdirectory(libs/core)
add_subdirectory(libs/link)
add_subdirectory(libs/telemetry)
add_subdirectory(libs/sched)
add_subdirectory(apps/groundd)

if(ORBITKIT_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()
