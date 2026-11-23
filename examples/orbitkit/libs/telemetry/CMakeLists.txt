add_library(orbit_telemetry STATIC
  src/Decoder.cpp
  src/CcsdsDecoder.cpp
  src/Ax25Decoder.cpp
  src/Sink.cpp
  src/Pipeline.cpp
)
add_library(orbitkit::telemetry ALIAS orbit_telemetry)
target_include_directories(orbit_telemetry PUBLIC include PRIVATE src)
target_link_libraries(orbit_telemetry PUBLIC orbit_core orbit_link)
