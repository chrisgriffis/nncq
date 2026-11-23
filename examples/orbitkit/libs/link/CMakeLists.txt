add_library(orbit_link STATIC
  src/Radio.cpp
  src/SdrRadio.cpp
  src/LoopbackRadio.cpp
  src/Antenna.cpp
)
add_library(orbitkit::link ALIAS orbit_link)
target_include_directories(orbit_link PUBLIC include)
target_link_libraries(orbit_link PUBLIC orbit_core)
