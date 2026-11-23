# Architecture

## One tick

`groundd` ticks once per second. `Station::serve` calls `Scheduler::tick`, which

1. picks the pass to work (`pickNext`: highest priority, then highest elevation),
2. asks the `Tracker` to point the `Antenna` at the satellite's current look angle,
3. retunes the radio for the Doppler shift computed by the `Ephemeris`,
4. while the pass is in its tracking phase and the tracker is locked, runs the telemetry `Pipeline`.

`Pipeline::run` drains up to 32 frames from the radio. For each one the decoder (`IDecoder::decode`) finds the
sync marker, checks the CRC-16 (`Crc::check`) and parses the body into packets. Good packets fan out to every
sink; frames that fail the CRC are counted in `Pipeline::Stats::crcErrors` and dropped.

## Decoders

```
IDecoder
 └─ FramedDecoder          sync marker + CRC-16, then decodeBody()
     ├─ CcsdsDecoder       CCSDS TM frames carrying space packets
     └─ Ax25Decoder        AX.25 UI frames, filtered by callsign
         └─ BeaconDecoder  plain-text status beacons
```

The Python package mirrors this hierarchy (`orbitkit_tools.decoders`) so archived passes can be re-decoded
offline with different settings.

## Known wart

`Scheduler.h` and `Tracker.h` include each other: the tracker keeps a back reference to its scheduler to report
a lost satellite (`Scheduler::onTrackerLost`). It only works because `Scheduler.h` is always included first.
A callback interface would break the cycle.
