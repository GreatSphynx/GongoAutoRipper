"""Grab a few preview frames from DVD titles so they can be shown before ripping.

A slice of the title's VOB files is read off the disc and decoded with OpenCV.
Unprotected discs are read straight from the VOB files. CSS-protected discs are
scrambled, so for those the sectors are read through VideoLAN's libdvdcss
(see vendor/libdvdcss), which unscrambles them.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import subprocess
import threading
import urllib.request
from pathlib import Path

import dvdnav
import platforms
import udf
from platforms import CREATE_NO_WINDOW, find_libdvdcss

os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")  # keep decoder warnings quiet
try:
    import cv2
    import numpy as np
except ImportError:  # previews are optional
    cv2 = np = None

SECTOR = dvdnav.SECTOR
FRACTIONS = (0.1, 0.5, 0.9)
CHUNK_SECTORS = 1024           # 2 MB: a few GOPs, enough to reach a decodable frame
FRAME_WIDTH = 320
DVDCSS_NOFLAGS, DVDCSS_SEEK_KEY, DVDCSS_READ_DECRYPT = 0, 2, 1


LIBDVDCSS_DOWNLOAD = "https://download.videolan.org/pub/libdvdcss/1.2.12/win64/"
LIBDVDCSS_SHA256 = "bced7a564c1f52b8cb2d0feb2a95aaad18c5db2483ff50bbfc37500174691a86"


def install_libdvdcss() -> Path:
    """Download VideoLAN's Windows libdvdcss into the user's settings folder. Raises OSError on failure."""
    request = urllib.request.Request(LIBDVDCSS_DOWNLOAD + "libdvdcss-2.dll", headers={"User-Agent": "GongoAutoRipper"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != LIBDVDCSS_SHA256:
        raise OSError("the downloaded file didn't match the expected checksum, so it wasn't installed")
    folder = platforms.user_lib_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "libdvdcss-2.dll"
    path.write_bytes(data)
    return path


class CopyProtected(Exception):
    pass


class DvdCss:
    """Unscrambled sector reads from a CSS-protected disc, through libdvdcss."""

    def __init__(self, dll: str, device: str):
        os.environ.setdefault("DVDCSS_CACHE", "off")
        os.environ.setdefault("DVDCSS_VERBOSE", "0")
        lib = ctypes.CDLL(dll)
        lib.dvdcss_open.restype = ctypes.c_void_p
        lib.dvdcss_open.argtypes = [ctypes.c_char_p]
        lib.dvdcss_seek.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.dvdcss_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.dvdcss_close.argtypes = [ctypes.c_void_p]
        lib.dvdcss_is_scrambled.argtypes = [ctypes.c_void_p]
        self.lib = lib
        self.handle = lib.dvdcss_open(device.encode())
        if not self.handle:
            raise OSError("libdvdcss couldn't open the disc")
        self.key_sector: int | None = None

    @property
    def scrambled(self) -> bool:
        return bool(self.lib.dvdcss_is_scrambled(self.handle))

    def read(self, key_sector: int, sector: int, count: int) -> bytes:
        """Read sectors, first fetching the title key for the title set starting at key_sector."""
        if key_sector != self.key_sector:
            if self.lib.dvdcss_seek(self.handle, key_sector, DVDCSS_SEEK_KEY) < 0:
                raise OSError("libdvdcss couldn't get the title key")
            self.key_sector = key_sector
        if self.lib.dvdcss_seek(self.handle, sector, DVDCSS_NOFLAGS) < 0:
            return b""
        buffer = ctypes.create_string_buffer(count * SECTOR)
        got = self.lib.dvdcss_read(self.handle, buffer, count, DVDCSS_READ_DECRYPT)
        return buffer.raw[:max(got, 0) * SECTOR]

    def raw_read(self, sector: int, count: int) -> bytes:
        """Read sectors as they are on the disc, e.g. the file system, which isn't scrambled."""
        if self.lib.dvdcss_seek(self.handle, sector, DVDCSS_NOFLAGS) < 0:
            return b""
        buffer = ctypes.create_string_buffer(count * SECTOR)
        got = self.lib.dvdcss_read(self.handle, buffer, count, DVDCSS_NOFLAGS)
        return buffer.raw[:max(got, 0) * SECTOR]

    def close(self) -> None:
        if self.handle:
            self.lib.dvdcss_close(self.handle)
            self.handle = None


def file_lba(path: Path) -> int | None:
    """The disc sector where a file starts, as Windows' UDF driver reports it.

    The sector numbers inside a DVD's IFO files are relative and can't always be
    trusted on protected discs, so the file system is the reliable source.
    """
    if not platforms.WINDOWS:
        return None
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
    handle = k32.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        return None
    try:
        start_vcn = ctypes.c_longlong(0)
        out = ctypes.create_string_buffer(64)
        got = wintypes.DWORD()
        ok = k32.DeviceIoControl(handle, FSCTL_GET_RETRIEVAL_POINTERS, ctypes.byref(start_vcn), 8,
                                 out, len(out), ctypes.byref(got), None)
        # RETRIEVAL_POINTERS_BUFFER: extent count, padding, starting VCN, then (next VCN, LCN) pairs.
        return struct.unpack_from("<q", out, 24)[0] if ok or got.value >= 32 else None
    finally:
        k32.CloseHandle(handle)


class CssVobs:
    """A title set's VOBs on a protected disc, addressed like VobSet (sectors from the first VOB)."""

    def __init__(self, css: DvdCss, vobs: VobSet):
        self.css = css
        self.files = []  # (first sector within the set, sector count, sector on the disc)
        file_system = None
        for path, start, count in vobs.files:
            # Windows can say where a file starts; elsewhere, read the disc's file system for it.
            lba = file_lba(path)
            if lba is None:
                try:
                    file_system = file_system or udf.Udf(css.raw_read)
                    lba = file_system.file_sector(f"VIDEO_TS/{path.name}")
                except (udf.UdfError, struct.error) as e:
                    raise CopyProtected(f"couldn't read the disc's file system: {e}") from e
            if lba is None:
                raise CopyProtected(f"couldn't find where {path.name} is on the disc")
            self.files.append((start, count, lba))

    def read(self, sector: int, count: int) -> bytes:
        out = bytearray()
        while count > 0:
            for start, n, lba in self.files:
                if start <= sector < start + n:
                    take = min(count, start + n - sector)
                    out += self.css.read(self.files[0][2], lba + sector - start, take)
                    sector += take
                    count -= take
                    break
            else:
                break
        return bytes(out)


def is_scrambled(data: bytes) -> bool:
    """True if the first video packet in the data has its CSS scrambling bits set."""
    for off in range(0, len(data) - SECTOR + 1, SECTOR):
        s = data[off:off + SECTOR]
        if s[14:18] == b"\x00\x00\x01\xe0":  # video PES straight after the pack header
            return bool((s[20] >> 4) & 3)
    return False


def from_first_vobu(data: bytes) -> bytes:
    """Start at the first navigation pack, where a fresh group of pictures begins."""
    for off in range(0, len(data) - SECTOR + 1, SECTOR):
        if data[off + 0x26:off + 0x2A] == b"\x00\x00\x01\xbf" and data[off + 0x2C] == 0:
            return data[off:]
    return data


class VobSet:
    """The VTS_nn_1.VOB, VTS_nn_2.VOB... files of a title set, read as one run of sectors."""

    def __init__(self, folder: Path, vts: int):
        self.files: list[tuple[Path, int, int]] = []
        start = 0
        for i in range(1, 10):
            path = folder / f"VTS_{vts:02}_{i}.VOB"
            if not path.exists():
                break
            count = path.stat().st_size // SECTOR
            self.files.append((path, start, count))
            start += count

    def read(self, sector: int, count: int) -> bytes:
        out = bytearray()
        while count > 0:
            for path, start, n in self.files:
                if start <= sector < start + n:
                    take = min(count, start + n - sector)
                    with open(path, "rb") as f:
                        f.seek((sector - start) * SECTOR)
                        out += f.read(take * SECTOR)
                    sector += take
                    count -= take
                    break
            else:
                break
        return bytes(out)


SETTLE_FRAMES = 18  # one DVD group of pictures


def _pick_frame(cap):
    """The first frame that isn't (nearly) black or flat, or the last one decoded.

    The slice can start part-way through a group of pictures, and frames that refer
    back to pictures before the slice decode as grey blocks. So the first group of
    pictures is skipped, by which point the decoder has a full picture to work from.
    """
    last = None
    for n in range(SETTLE_FRAMES + 90):
        ok, frame = cap.read()
        if not ok:
            break
        last = frame
        if n >= SETTLE_FRAMES and frame.mean() > 15 and frame.std() > 8:
            return frame
    return last


def _keyframe(ffmpeg: str, chunk: Path):
    """The first keyframe in the chunk that isn't (nearly) black, decoded by ffmpeg.

    Keyframes are complete pictures, so unlike the frames between them they can't come
    out as grey blocks when the chunk starts part-way through a group of pictures.
    """
    for old in chunk.parent.glob("key*.png"):
        old.unlink()
    subprocess.run([ffmpeg, "-v", "error", "-y", "-skip_frame", "nokey", "-i", str(chunk), "-fps_mode", "passthrough",
                    "-frames:v", "4", str(chunk.with_name("key%d.png"))],
                   capture_output=True, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    frames = []
    for path in sorted(chunk.parent.glob("key*.png")):
        frame = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        path.unlink()
        if frame is not None:
            frames.append(frame)
    return next((f for f in frames if f.mean() > 15 and f.std() > 8), frames[-1] if frames else None)


class PreviewGrabber:
    def __init__(self, device: str, folder: Path, ffmpeg: str = ""):
        if cv2 is None:
            raise ImportError("OpenCV (opencv-python) isn't installed")
        self.ffmpeg = ffmpeg
        self.device = device.rstrip("\\")
        root = platforms.disc_root(device)
        if root is None:
            raise OSError("the disc isn't mounted")
        self.disc = dvdnav.Disc(root)
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.scrambled = False
        self._css: DvdCss | None = None
        # Ask libdvdcss up front whether the disc is scrambled. Finding out by reading
        # scrambled video through Windows first can leave the drive returning bad data
        # for some later reads.
        if dll := find_libdvdcss():
            try:
                self._css = DvdCss(dll, platforms.css_device(self.device))
                self.scrambled = self._css.scrambled
            except OSError:
                self._css = None
            if self._css and not self.scrambled:
                self.close()

    def close(self) -> None:
        if self._css:
            self._css.close()
            self._css = None

    def _ifo(self, vts: int) -> bytes:
        return (self.disc.dir / f"VTS_{vts:02}_0.IFO").read_bytes()

    def _reader(self, entry: dvdnav.TitleEntry):
        """Where to read a title set's video from: the VOB files, or libdvdcss once the disc proves scrambled."""
        if not self.scrambled:
            return VobSet(self.disc.dir, entry.vts)
        if self._css is None:
            dll = find_libdvdcss()
            if not dll:
                raise CopyProtected(
                    "libdvdcss isn't installed. To see previews of protected DVDs, untick and tick "
                    "Show preview frames and choose Download and install" if platforms.WINDOWS else
                    f"libdvdcss isn't installed. To see previews of protected DVDs, install it: {platforms.LIBDVDCSS_HINT}")
            try:
                self._css = DvdCss(dll, platforms.css_device(self.device))
            except OSError as e:
                raise CopyProtected(str(e)) from e
        return CssVobs(self._css, VobSet(self.disc.dir, entry.vts))

    def frames(self, title: int, stop: threading.Event) -> list[Path]:
        """Save up to three frames (start, middle, end) of a DVD title; returns their paths.
        Raises CopyProtected if the disc is scrambled and libdvdcss can't unscramble it."""
        entry = self.disc.titles[title - 1]
        vts = self.disc.vts(entry.vts)
        cells = []
        for pgcn in dict.fromkeys(p for p, _ in vts.ptt[entry.vts_ttn - 1]):
            cells += vts.pgcs[pgcn - 1].cells
        total = sum(c.duration for c in cells)
        if not cells or total <= 0:
            return []
        widescreen = (self._ifo(entry.vts)[0x200] >> 2) & 3 == 3
        reader = self._reader(entry)
        saved = []
        for i, fraction in enumerate(FRACTIONS):
            if stop.is_set():
                break
            path = self.folder / f"title{title:03}_{i}.png"
            try:
                ok = self._grab(reader, cells, fraction * total, path, widescreen)
            except CopyProtected:
                if self.scrambled:  # libdvdcss's output is still scrambled
                    raise CopyProtected("libdvdcss couldn't unscramble this disc")
                self.scrambled = True
                reader = self._reader(entry)
                ok = self._grab(reader, cells, fraction * total, path, widescreen)
            if ok:
                saved.append(path)
        return saved

    def _grab(self, vobs, cells, target: float, path: Path, widescreen: bool) -> bool:
        elapsed = 0.0
        for cell in cells:
            if elapsed + cell.duration >= target or cell is cells[-1]:
                within = (target - elapsed) / cell.duration if cell.duration else 0
                sector = cell.first_sector + int((cell.last_sector - cell.first_sector) * min(max(within, 0), 1))
                break
            elapsed += cell.duration
        sector = min(sector, max(cell.first_sector, cell.last_sector - CHUNK_SECTORS))
        data = vobs.read(sector, CHUNK_SECTORS)
        if not data:
            return False
        if is_scrambled(data):
            raise CopyProtected
        chunk = self.folder / "chunk.vob"
        chunk.write_bytes(from_first_vobu(data))
        try:
            if self.ffmpeg:
                frame = _keyframe(self.ffmpeg, chunk)
            else:
                cap = cv2.VideoCapture(str(chunk))
                try:
                    frame = _pick_frame(cap)
                finally:
                    cap.release()
        finally:
            chunk.unlink(missing_ok=True)
        if frame is None:
            return False
        height = FRAME_WIDTH * 9 // 16 if widescreen else FRAME_WIDTH * 3 // 4
        small = cv2.resize(frame, (FRAME_WIDTH, height), interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", small)
        if ok:
            path.write_bytes(png.tobytes())
        return ok
