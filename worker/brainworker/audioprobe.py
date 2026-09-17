"""How long a recording is, read from its own headers and nothing else.

The quote at the gate is `duration × $0.024/min`, and it has to exist before
anything is downloaded — the object may be a gigabyte and the point of the
gate is to say the number first. The worker image has no ffmpeg and no
compiler, by decision (`doc/VIDEO.md`, *No ffmpeg, and that is measured*), so
this reads what a container's own headers state:

- **MP3**: the first frame header gives version, layer, bitrate and sample
  rate; a `Xing`/`Info` or `VBRI` frame gives the total frame count, which is
  the exact duration for a VBR file. Without one, `bytes × 8 / bitrate` is
  exact for CBR and an estimate for VBR — and the estimate is *flagged*,
  because a quote built on it is a quote the product cannot stand behind.
- **MP4/M4A**: the `mvhd` atom inside `moov` carries a timescale and a
  duration. `moov` is written first by some encoders and last by others, so a
  miss in the head reads the tail once.
- **FLAC**: `STREAMINFO` carries the sample rate and total samples.
- **WAV**: the `fmt` chunk's byte rate and the `data` chunk's size.
- **Ogg and WebM**: no cheap header; estimated from size and flagged.

Everything here is pure over `bytes`, so the tests plant a hand-built Xing
frame or an `mvhd` atom and the real measurement is the corpus — 147 objects
whose durations the sync prints beside the manifest's own `mb` column, and
whose billed seconds the pilot compares against these figures.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: When nothing better is known, assume this bitrate — the *low* end of what
#: spoken-word audio is encoded at, so the estimate errs long and the quote
#: errs high, which is the permitted direction. A flagged estimate is never
#: the number a person is asked to approve without the flag beside it.
ESTIMATE_KBPS = 64


@dataclass(frozen=True)
class Probed:
    container: str
    #: Seconds, or 0 when nothing could be read at all.
    duration_s: float
    #: True when `duration_s` came from the size and an assumed or averaged
    #: bitrate rather than from a frame count or a duration field.
    estimated: bool
    #: How it was read, for the probe artifact: `xing`, `vbri`, `cbr`,
    #: `vbr-average`, `mvhd`, `streaminfo`, `wav`, `size`.
    method: str


def probe(
    container: str, head: bytes, size: int, tail: bytes = b"", body: bytes = b""
) -> Probed:
    """The duration for a container `s3source.sniff_container` named.

    `body` is a second read starting where an ID3 tag ends, for the MP3 whose
    tag — cover art, usually — outran the head. Measured on the first real
    bucket: one file of 147 carried an 88 KB tag against a 64 KB head, and
    without this its duration was a size estimate flagged as such.
    """
    if container == "mp3":
        seconds, estimated, method = mp3_duration(head, size, body or None)
        return Probed("mp3", seconds, estimated, method)
    if container == "mp4":
        seconds = mp4_duration(head) or mp4_duration(tail)
        if seconds is not None:
            return Probed("mp4", seconds, False, "mvhd")
        return Probed("mp4", _from_size(size), True, "size")
    if container == "flac":
        seconds = flac_duration(head)
        if seconds is not None:
            return Probed("flac", seconds, False, "streaminfo")
        return Probed("flac", _from_size(size), True, "size")
    if container == "wav":
        seconds = wav_duration(head, size)
        if seconds is not None:
            return Probed("wav", seconds, False, "wav")
        return Probed("wav", _from_size(size), True, "size")
    return Probed(container, _from_size(size), True, "size")


def needs_tail(container: str, head: bytes) -> bool:
    """Whether the head left the question open and the tail might close it.

    Only an MP4 whose `moov` is not in the head. Everything else is settled by
    the head or not settled by any amount of tail.
    """
    return container == "mp4" and mp4_duration(head) is None


def needs_body(container: str, head: bytes) -> int:
    """Where a second read should start, or 0 when the head suffices.

    Only an MP3 whose ID3 tag is larger than the head: the frames begin at
    the tag's end and nothing in the head can say how long they are.
    """
    if container != "mp3":
        return 0
    tag = id3v2_size(head)
    return tag if tag >= len(head) - 4 else 0


def _from_size(size: int) -> float:
    return size * 8 / (ESTIMATE_KBPS * 1000) if size > 0 else 0.0


# --- MP3 ----------------------------------------------------------------------

_BITRATES = {
    # (version_group, layer): index 1..14 in kbps. Group 1 = MPEG-1, 2 = MPEG-2/2.5.
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),  # MPEG-1
    2: (22050, 24000, 16000),  # MPEG-2
    0: (11025, 12000, 8000),   # MPEG-2.5
}


@dataclass(frozen=True)
class Frame:
    offset: int
    version: int      # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
    layer: int        # 1, 2 or 3
    bitrate_kbps: int
    sample_rate: int
    padding: int
    mono: bool

    @property
    def samples(self) -> int:
        if self.layer == 1:
            return 384
        if self.layer == 2 or self.version == 3:
            return 1152
        return 576

    @property
    def length(self) -> int:
        if self.layer == 1:
            return (12 * self.bitrate_kbps * 1000 // self.sample_rate + self.padding) * 4
        per = 144 if (self.layer == 2 or self.version == 3) else 72
        return per * self.bitrate_kbps * 1000 // self.sample_rate + self.padding

    @property
    def side_info(self) -> int:
        """Bytes between the header and the Xing tag for Layer III."""
        if self.version == 3:
            return 17 if self.mono else 32
        return 9 if self.mono else 17


def id3v2_size(head: bytes) -> int:
    """Bytes occupied by an ID3v2 tag at the start, or 0."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    flags = head[5]
    size = 0
    for b in head[6:10]:
        size = (size << 7) | (b & 0x7F)
    return 10 + size + (10 if flags & 0x10 else 0)


def parse_frame(head: bytes, offset: int) -> Frame | None:
    if offset + 4 > len(head):
        return None
    b1, b2, b3 = head[offset + 1], head[offset + 2], head[offset + 3]
    if head[offset] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 3
    layer_bits = (b1 >> 1) & 3
    if version == 1 or layer_bits == 0:
        return None
    layer = {1: 3, 2: 2, 3: 1}[layer_bits]
    br_index = b2 >> 4
    sr_index = (b2 >> 2) & 3
    if br_index in (0, 15) or sr_index == 3:
        return None
    group = 1 if version == 3 else 2
    bitrate = _BITRATES[(group, layer)][br_index - 1]
    sample_rate = _SAMPLE_RATES[version][sr_index]
    return Frame(
        offset=offset, version=version, layer=layer, bitrate_kbps=bitrate,
        sample_rate=sample_rate, padding=(b2 >> 1) & 1, mono=((b3 >> 6) & 3) == 3,
    )


def first_frame(head: bytes, start: int) -> Frame | None:
    """The first header that is followed by another valid header, so a stray
    0xFF inside a tag's padding does not read as a frame."""
    i = start
    limit = len(head) - 4
    while i < limit:
        f = parse_frame(head, i)
        if f is not None and f.length > 0:
            nxt = f.offset + f.length
            if nxt + 4 > len(head) or parse_frame(head, nxt) is not None:
                return f
        i += 1
    return None


def mp3_duration(
    head: bytes, size: int, body: bytes | None = None
) -> tuple[float, bool, str]:
    tag = id3v2_size(head)
    if body is not None:
        # The frames were read separately, starting at the tag's end; scan
        # them from their own offset zero.
        head, start = body, 0
    else:
        start = tag
    f = first_frame(head, start)
    if f is None:
        return _from_size(size), True, "size"

    # Xing / Info: exact for VBR, and present on most CBR files too.
    at = f.offset + 4 + f.side_info
    if head[at:at + 4] in (b"Xing", b"Info") and len(head) >= at + 12:
        flags = struct.unpack(">I", head[at + 4:at + 8])[0]
        if flags & 1:
            frames = struct.unpack(">I", head[at + 8:at + 12])[0]
            if frames > 0:
                return frames * f.samples / f.sample_rate, False, "xing"
    # VBRI (Fraunhofer): 32 bytes after the header, frames at +14.
    at = f.offset + 4 + 32
    if head[at:at + 4] == b"VBRI" and len(head) >= at + 18:
        frames = struct.unpack(">I", head[at + 14:at + 18])[0]
        if frames > 0:
            return frames * f.samples / f.sample_rate, False, "vbri"

    # No frame count. Walk the frames the head holds: all one bitrate is CBR
    # and the arithmetic is exact; a spread is VBR without a header, and the
    # average of what was seen is an estimate that says so.
    rates: list[int] = []
    cur: Frame | None = f
    while cur is not None and len(rates) < 200:
        rates.append(cur.bitrate_kbps)
        nxt = cur.offset + cur.length
        cur = parse_frame(head, nxt) if nxt + 4 <= len(head) else None
    audio_bytes = max(size - tag, 0)
    if len(set(rates)) == 1:
        return audio_bytes * 8 / (rates[0] * 1000), False, "cbr"
    average = sum(rates) / len(rates)
    return audio_bytes * 8 / (average * 1000), True, "vbr-average"


# --- MP4 / M4A ----------------------------------------------------------------


def mp4_duration(data: bytes) -> float | None:
    """Seconds from `moov/mvhd`, or `None` when the buffer holds no `moov`."""
    if not data:
        return None
    for start, end, kind in _atoms(data, 0, len(data)):
        if kind == b"moov":
            for s2, e2, k2 in _atoms(data, start + 8, end):
                if k2 == b"mvhd":
                    return _mvhd(data, s2 + 8, e2)
    # A tail buffer usually starts mid-atom; the walk above then reads
    # garbage sizes. Fall back to finding `moov` by name and walking from it.
    at = data.rfind(b"moov")
    if at >= 4:
        for s2, e2, k2 in _atoms(data, at + 4, len(data)):
            if k2 == b"mvhd":
                return _mvhd(data, s2 + 8, e2)
    return None


def _atoms(data: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        header = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            header = 16
        elif size == 0:
            size = end - i
        if size < header:
            return
        yield i, min(i + size, end), kind
        i += size


def _mvhd(data: bytes, at: int, end: int) -> float | None:
    if at + 4 > end:
        return None
    version = data[at]
    try:
        if version == 1:
            timescale = struct.unpack(">I", data[at + 20:at + 24])[0]
            duration = struct.unpack(">Q", data[at + 24:at + 32])[0]
        else:
            timescale = struct.unpack(">I", data[at + 12:at + 16])[0]
            duration = struct.unpack(">I", data[at + 16:at + 20])[0]
    except struct.error:
        return None
    if timescale <= 0:
        return None
    return duration / timescale


# --- FLAC and WAV -------------------------------------------------------------


def flac_duration(head: bytes) -> float | None:
    """`STREAMINFO` is always the first metadata block: 4 bytes of header
    after `fLaC`, then 34 bytes of which 18–25 pack the sample rate (20 bits),
    channels, bits per sample and total samples (36 bits)."""
    if head[:4] != b"fLaC" or len(head) < 8 + 34:
        return None
    block = head[8:8 + 34]
    packed = int.from_bytes(block[10:18], "big")
    sample_rate = packed >> 44
    total = packed & ((1 << 36) - 1)
    if sample_rate <= 0 or total <= 0:
        return None
    return total / sample_rate


def wav_duration(head: bytes, size: int) -> float | None:
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    i = 12
    byte_rate = 0
    while i + 8 <= len(head):
        kind = head[i:i + 4]
        length = struct.unpack("<I", head[i + 4:i + 8])[0]
        if kind == b"fmt " and i + 8 + 16 <= len(head):
            byte_rate = struct.unpack("<I", head[i + 16:i + 20])[0]
        elif kind == b"data":
            if byte_rate <= 0:
                return None
            data_bytes = length if length > 0 else max(size - i - 8, 0)
            return data_bytes / byte_rate
        i += 8 + length + (length & 1)
    return None
