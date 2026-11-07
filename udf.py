"""Find where files start on a DVD by reading its UDF file system directly.

Previews of CSS-protected DVDs read the video through libdvdcss, which works in disc
sectors, so the start sector of each VOB file is needed. Windows can report that for a
file; other systems can't without root. This reads the UDF structures instead (ECMA-167,
as used by DVD-Video's UDF 1.02), the way libdvdread does. The file system sectors aren't
scrambled, so plain reads of them work on protected discs too.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

SECTOR = 2048
ANCHOR_SECTOR = 256

TAG_ANCHOR, TAG_PARTITION, TAG_LOGICAL_VOLUME, TAG_TERMINATOR = 2, 5, 6, 8
TAG_FILE_SET, TAG_FILE_ID, TAG_FILE_ENTRY, TAG_EXTENDED_FILE_ENTRY = 256, 257, 261, 266
FID_DIRECTORY, FID_DELETED, FID_PARENT = 0x02, 0x04, 0x08

Reader = Callable[[int, int], bytes]   # (first sector, sector count) -> bytes


class UdfError(Exception):
    pass


def _tag(data: bytes) -> int:
    return struct.unpack_from("<H", data, 0)[0] if len(data) >= 16 else -1


def _name(raw: bytes) -> str:
    """A UDF d-string: the first byte says 8-bit or 16-bit (big-endian) characters."""
    if not raw:
        return ""
    if raw[0] == 16:
        return raw[1:].decode("utf-16-be", errors="replace")
    return raw[1:].decode("latin-1")


class Udf:
    def __init__(self, read: Reader):
        self.read = read
        anchor = read(ANCHOR_SECTOR, 1)
        if _tag(anchor) != TAG_ANCHOR:
            raise UdfError("no UDF anchor at sector 256")
        vds_length, vds_start = struct.unpack_from("<II", anchor, 16)
        self.partition_start = None
        fsd_lbn = None
        for i in range(max(1, vds_length // SECTOR)):
            d = read(vds_start + i, 1)
            tag = _tag(d)
            if tag == TAG_PARTITION and self.partition_start is None:
                self.partition_start = struct.unpack_from("<I", d, 188)[0]
            elif tag == TAG_LOGICAL_VOLUME:
                fsd_lbn = struct.unpack_from("<I", d, 252)[0]   # long_ad of the file set descriptor
            elif tag == TAG_TERMINATOR:
                break
        if self.partition_start is None or fsd_lbn is None:
            raise UdfError("no partition or logical volume descriptor")
        fsd = self._block(fsd_lbn)
        if _tag(fsd) != TAG_FILE_SET:
            raise UdfError("no file set descriptor")
        self.root = struct.unpack_from("<I", fsd, 404)[0]   # root directory ICB (long_ad at 400)

    def _block(self, lbn: int, count: int = 1) -> bytes:
        return self.read(self.partition_start + lbn, count)

    def _entry(self, icb: int) -> tuple[list[tuple[int, int]], int, bytes]:
        """A file entry's extents as (logical block, bytes), its size, and any data stored inline."""
        fe = self._block(icb)
        tag = _tag(fe)
        if tag == TAG_FILE_ENTRY:
            ea_length, ad_length = struct.unpack_from("<II", fe, 168)
            ads = 176 + ea_length
        elif tag == TAG_EXTENDED_FILE_ENTRY:
            ea_length, ad_length = struct.unpack_from("<II", fe, 208)
            ads = 216 + ea_length
        else:
            raise UdfError(f"no file entry at block {icb}")
        size = struct.unpack_from("<Q", fe, 56)[0]
        kind = struct.unpack_from("<H", fe, 34)[0] & 7   # ICB tag flags: how extents are recorded
        extents = []
        if kind == 0:     # short_ad: length, position
            for off in range(ads, ads + ad_length - 7, 8):
                length, lbn = struct.unpack_from("<II", fe, off)
                if length & 0x3FFFFFFF:
                    extents.append((lbn, length & 0x3FFFFFFF))
        elif kind == 1:   # long_ad: length, position, partition, implementation use
            for off in range(ads, ads + ad_length - 15, 16):
                length, lbn = struct.unpack_from("<II", fe, off)
                if length & 0x3FFFFFFF:
                    extents.append((lbn, length & 0x3FFFFFFF))
        elif kind == 3:   # the data is inside the entry itself
            return [], size, fe[ads:ads + ad_length]
        return extents, size, b""

    def _directory(self, icb: int) -> dict[str, tuple[int, bool]]:
        """Name (upper case) -> (ICB block, is a directory) for each entry in a directory."""
        extents, size, inline = self._entry(icb)
        data = inline or b"".join(self._block(lbn, -(-length // SECTOR))[:length] for lbn, length in extents)
        entries = {}
        off = 0
        while off + 38 <= min(len(data), size or len(data)):
            if _tag(data[off:off + 16]) != TAG_FILE_ID:
                break
            flags, name_length = data[off + 18], data[off + 19]
            child = struct.unpack_from("<I", data, off + 24)[0]
            iu_length = struct.unpack_from("<H", data, off + 36)[0]
            start = off + 38 + iu_length
            if not flags & (FID_DELETED | FID_PARENT):
                entries[_name(data[start:start + name_length]).upper()] = (child, bool(flags & FID_DIRECTORY))
            off += (38 + iu_length + name_length + 3) & ~3
        return entries

    def file_sector(self, path: str) -> int | None:
        """The disc sector a file starts at, e.g. file_sector("VIDEO_TS/VTS_01_1.VOB"), or None."""
        icb, is_dir = self.root, True
        for part in path.strip("/").split("/"):
            if not is_dir:
                return None
            found = self._directory(icb).get(part.upper())
            if found is None:
                return None
            icb, is_dir = found
        extents, _, _ = self._entry(icb)
        return self.partition_start + extents[0][0] if extents else None
