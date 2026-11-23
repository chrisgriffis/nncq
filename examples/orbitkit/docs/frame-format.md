# Frame format

orbitkit decodes CCSDS-style TM transfer frames (default length 223 bytes) behind the attached sync marker
`1A CF FC 1D`:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | version (2 bits), spacecraft id (10 bits), virtual channel (3 bits), OCF flag |
| 2 | 1 | master channel frame count (`sequence`) |
| 3 | 1 | virtual channel frame count |
| 4 | 2 | data field status |
| 6 | n | data field: space packets, back to back |
| 6+n | 2 | CRC-16/CCITT-FALSE over everything before it |

Virtual channel 7 carries idle frames. Inside the data field, each space packet starts with a 6-byte header
(APID, sequence count, length - 1). APID `0x7FF` is the idle packet and fills the rest of the field.

A frame whose CRC does not match is dropped as a whole: there is no forward error correction in this version.
Sequence gaps are counted per decoder (`CcsdsDecoder::sequenceGaps_`) and show up in pass reports.

## Archive files (`.okp`)

`FileSink` writes `[apid:2][length:2][data]` per packet and a CRC-32 trailer over the whole file.
`orbit-tools report <file>.okp` reads them back.
