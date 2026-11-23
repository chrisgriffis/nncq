add_library(orbit_core STATIC
  src/Clock.cpp
  src/Crc.cpp
  src/Frame.cpp
  src/Log.cpp
)
add_library(orbitkit::core ALIAS orbit_core)
target_include_directories(orbit_core PUBLIC include)
