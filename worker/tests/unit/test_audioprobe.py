"""The duration reader, on bytes built by hand.

No fixture file: the worker image has no encoder and neither does this test
suite, so every container here is assembled from its own specification. What
that buys is precision — a Xing frame that says 2,000 frames *is* 52.2 s at
44.1 kHz, and the assertion is that arithmetic, not a tolerance around a file
somebody encoded once.
"""

from __future__ import annotations

import struct

from brainworker import audioprobe
from brainworker.audioprobe import (
    ESTIMATE_KBPS,
    first_frame,
    flac_duration,
    id3v2_size,
    mp3_duration,
    mp4_duration,
    parse_frame,
    probe,
    wav_duration,
)
from brainworker.s3source import sniff_container


# --- MP3 --------------------------------------------------------------------


def mp3_header(*, bitrate_index: int = 9, sample_index: int = 0, padding: int = 0,
               mono: bool = False, version: int = 3) -> bytes:
    """A four-byte MPEG frame header. Defaults: MPEG-1 Layer III, 128 kbps,
    44.1 kHz, joint stereo."""
    b1 = 0xE0 | (version << 3) | (1 << 1) | 1     # sync, version, layer III, no CRC
    b2 = (bitrate_index << 4) | (sample_index << 2) | (padding << 1)
    b3 = (0b11 if mono else 0b01) << 6
    return bytes([0xFF, b1, b2, b3])


def cbr_stream(frames: int, **kw) -> bytes:
    header = mp3_header(**kw)
    f = parse_frame(header + b"\x00" * 4, 0)
    assert f is not None
    body = b"\x00" * (f.length - 4)
    return (header + body) * frames


def id3(size: int) -> bytes:
    """An ID3v2.3 tag of `size` payload bytes, with the size in syncsafe form."""
    sync = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + sync + b"\x00" * size


def test_a_frame_header_parses_to_the_published_tables():
    f = parse_frame(mp3_header() + b"\x00" * 4, 0)
    assert f is not None
    assert (f.version, f.layer, f.bitrate_kbps, f.sample_rate) == (3, 3, 128, 44100)
    assert f.samples == 1152
    # 144 * 128000 / 44100 = 417.9 -> 417 bytes, no padding
    assert f.length == 417
    assert f.side_info == 32


def test_an_id3_tag_is_skipped_before_the_first_frame():
    data = id3(3000) + cbr_stream(3)
    assert id3v2_size(data) == 3010
    f = first_frame(data, id3v2_size(data))
    assert f is not None and f.offset == 3010


def test_a_stray_sync_byte_inside_the_tag_is_not_a_frame():
    """`first_frame` demands a second valid header right after the first."""
    tag = bytearray(id3(100))
    tag[20:24] = mp3_header()  # a header-shaped pattern inside the padding
    data = bytes(tag) + cbr_stream(3)
    f = first_frame(data, 0)
    assert f is not None and f.offset == len(tag)


def test_xing_frame_gives_the_exact_duration_for_vbr():
    header = mp3_header()
    f = parse_frame(header + b"\x00" * 4, 0)
    assert f is not None
    frame = bytearray(f.length)
    frame[0:4] = header
    at = 4 + f.side_info
    frame[at:at + 4] = b"Xing"
    frame[at + 4:at + 8] = struct.pack(">I", 0x0F)
    frame[at + 8:at + 12] = struct.pack(">I", 2000)
    data = bytes(frame) + cbr_stream(2)
    seconds, estimated, method = mp3_duration(data, size=10_000_000)
    assert method == "xing" and estimated is False
    assert abs(seconds - 2000 * 1152 / 44100) < 1e-9
    # The size is irrelevant once the frame count is known.
    assert mp3_duration(data, size=1)[0] == seconds


def test_vbri_frame_is_read_too():
    header = mp3_header()
    f = parse_frame(header + b"\x00" * 4, 0)
    assert f is not None
    frame = bytearray(f.length)
    frame[0:4] = header
    at = 4 + 32
    frame[at:at + 4] = b"VBRI"
    frame[at + 14:at + 18] = struct.pack(">I", 500)
    data = bytes(frame) + cbr_stream(2)
    seconds, estimated, method = mp3_duration(data, size=1)
    assert method == "vbri" and estimated is False
    assert abs(seconds - 500 * 1152 / 44100) < 1e-9


def test_cbr_without_a_header_is_exact_from_the_size():
    data = id3(500) + cbr_stream(20)
    size = 500 + 10 + 128_000 // 8 * 60  # sixty seconds at 128 kbps, plus the tag
    seconds, estimated, method = mp3_duration(data, size=size)
    assert method == "cbr" and estimated is False
    assert abs(seconds - 60.0) < 1e-9


def test_vbr_without_a_header_is_an_estimate_and_says_so():
    a = cbr_stream(5, bitrate_index=9)     # 128 kbps
    b = cbr_stream(5, bitrate_index=5)     # 64 kbps
    seconds, estimated, method = mp3_duration(a + b, size=len(a + b))
    assert method == "vbr-average" and estimated is True
    assert seconds > 0


def test_no_frame_at_all_falls_back_to_size_and_flags_it():
    seconds, estimated, method = mp3_duration(b"\x00" * 100, size=8_000_000)
    assert (estimated, method) == (True, "size")
    assert abs(seconds - 8_000_000 * 8 / (ESTIMATE_KBPS * 1000)) < 1e-9


def test_the_estimate_errs_long_which_is_the_permitted_direction():
    """64 kbps is the low end of spoken-word encodes, so a size-only figure
    reads as more minutes, and the quote built on it as more dollars."""
    assert ESTIMATE_KBPS <= 96


# --- MP4 --------------------------------------------------------------------


def atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mvhd(timescale: int, duration: int, version: int = 0) -> bytes:
    if version == 1:
        body = b"\x01\x00\x00\x00" + b"\x00" * 16 + struct.pack(">IQ", timescale, duration)
    else:
        body = b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", timescale, duration)
    return atom(b"mvhd", body + b"\x00" * 80)


def test_mvhd_at_the_start_gives_the_duration():
    data = atom(b"ftyp", b"M4A \x00\x00\x00\x00") + atom(b"moov", mvhd(1000, 4_567_000))
    assert mp4_duration(data) == 4567.0
    assert sniff_container(data) == "mp4"


def test_mvhd_version_1_carries_a_64_bit_duration():
    data = atom(b"ftyp", b"M4A \x00\x00\x00\x00") + atom(b"moov", mvhd(48000, 48000 * 3661, version=1))
    assert mp4_duration(data) == 3661.0


def test_moov_written_last_is_found_in_a_tail_that_starts_mid_atom():
    """An encoder that writes `mdat` first leaves `moov` at the end, and a tail
    buffer begins somewhere inside `mdat`'s bytes — so the atom walk from
    offset 0 reads garbage and the reader has to find `moov` by name."""
    head = atom(b"ftyp", b"M4A \x00\x00\x00\x00") + struct.pack(">I", 8 + 5000) + b"mdat"
    media = b"\x7f" * 5000
    moov = atom(b"moov", mvhd(1000, 90_000))
    whole = head + media + moov
    assert mp4_duration(whole[:64]) is None, "the head holds no moov"
    assert audioprobe.needs_tail("mp4", whole[:64]) is True
    tail = whole[-300:]
    assert mp4_duration(tail) == 90.0


def test_an_mp4_with_no_moov_anywhere_is_estimated_and_flagged():
    data = atom(b"ftyp", b"M4A \x00\x00\x00\x00") + atom(b"free", b"\x00" * 10)
    p = probe("mp4", data, size=1_000_000, tail=b"")
    assert p.estimated is True and p.method == "size"


# --- FLAC and WAV ------------------------------------------------------------


def test_flac_streaminfo_gives_samples_over_rate():
    sample_rate, channels, bps, total = 44100, 2, 16, 44100 * 125
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    info = b"\x10\x00\x10\x00" + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    data = b"fLaC" + b"\x80\x00\x00\x22" + info
    assert flac_duration(data) == 125.0
    assert sniff_container(data) == "flac"


def test_wav_data_chunk_over_byte_rate():
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    data = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    data += b"data" + struct.pack("<I", 32000 * 42)
    assert wav_duration(data, size=len(data) + 32000 * 42) == 42.0
    assert sniff_container(data) == "wav"


# --- sniffing ----------------------------------------------------------------


def test_the_extension_is_never_what_decides():
    """An M4A wearing `.mp3` is the case the first corpus has four of."""
    m4a = atom(b"ftyp", b"M4A \x00\x00\x00\x00") + b"\x00" * 8
    assert sniff_container(m4a) == "mp4"
    assert sniff_container(id3(10) + cbr_stream(1)) == "mp3"
    assert sniff_container(cbr_stream(1)) == "mp3"
    assert sniff_container(b"OggS" + b"\x00" * 20) == "ogg"
    assert sniff_container(b"\x1a\x45\xdf\xa3" + b"\x00" * 20) == "webm"
    assert sniff_container(b"not audio at all, a text file") == ""
    assert sniff_container(b"") == ""


def test_an_id3_tag_larger_than_the_head_asks_for_a_second_read():
    """Measured on the first real bucket: one file of 147 carried an 88 KB
    tag of cover art against a 64 KB head, and read as a size estimate."""
    from brainworker.audioprobe import needs_body

    tag = id3(90_000)
    head = tag[:65536]
    assert needs_body("mp3", head) == 90_010
    assert needs_body("mp3", id3(100) + cbr_stream(3)) == 0
    assert needs_body("mp4", head) == 0
    body = cbr_stream(20)
    size = 90_010 + 128_000 // 8 * 30
    seconds, estimated, method = mp3_duration(head, size, body)
    assert method == "cbr" and estimated is False
    assert abs(seconds - 30.0) < 1e-9
    p = probe("mp3", head, size, body=body)
    assert p.method == "cbr" and p.estimated is False
