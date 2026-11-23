from orbitkit_tools import crc
from orbitkit_tools.decoders import CcsdsDecoder, DecodeStatus, make_decoder, BeaconDecoder


def _frame(seq: int, corrupt: bool = False) -> bytes:
    body = bytearray(32)
    body[0:3] = bytes([0x01, 0x20, seq])
    body[6:16] = bytes([0x00, 0x42, 0xC0, 0x01, 0x00, 0x03]) + b"ping"
    body[16:22] = bytes([0x07, 0xFF, 0xC0, 0x00, 0x00, 0x07])          # idle packet fills the rest
    sealed = bytearray(crc.seal(bytes(body[:-2])))
    if corrupt:
        sealed[10] ^= 0xFF
    return CcsdsDecoder.ASM + bytes(sealed)


def test_reference_vectors():
    assert crc.ccitt16(b"123456789") == 0x29B1
    assert crc.crc32(b"123456789") == 0xCBF43926


def test_ccsds_decoder_drops_corrupted_frames():
    d = CcsdsDecoder(32)
    ok = d.decode(_frame(1))
    assert ok.status is DecodeStatus.OK and [p.apid for p in ok.packets] == [0x042]
    assert d.decode(_frame(2, corrupt=True)).status is DecodeStatus.BAD_CRC
    assert d.crc_failures == 1


def test_make_decoder_and_beacon_fields():
    assert make_decoder("beacon:KST1").protocol == "ax25-beacon"
    assert make_decoder("morse").protocol == "ccsds-tm"
    assert BeaconDecoder.fields("KST1>BCN: batt=7.9V temp=12C") == {"batt": "7.9V", "temp": "12C"}
