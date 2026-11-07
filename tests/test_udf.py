import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import udf  # noqa: E402

PARTITION = 300


def sector(tag: int, fields: dict[int, bytes]) -> bytes:
    data = bytearray(udf.SECTOR)
    struct.pack_into("<H", data, 0, tag)
    for offset, value in fields.items():
        data[offset:offset + len(value)] = value
    return bytes(data)


def file_entry(lbn: int, size: int, tag=udf.TAG_FILE_ENTRY, long_ad=False) -> bytes:
    ads = 216 if tag == udf.TAG_EXTENDED_FILE_ENTRY else 176
    lengths = 208 if tag == udf.TAG_EXTENDED_FILE_ENTRY else 168
    ad = struct.pack("<IIH6x", size, lbn, 0) if long_ad else struct.pack("<II", size, lbn)
    return sector(tag, {34: struct.pack("<H", 1 if long_ad else 0), 56: struct.pack("<Q", size),
                        lengths: struct.pack("<II", 0, len(ad)), ads: ad})


def fid(name: str, icb: int, flags: int = 0, wide=False) -> bytes:
    raw = (b"\x10" + name.encode("utf-16-be")) if wide else (b"\x08" + name.encode("latin-1")) if name else b""
    body = bytearray(38 + len(raw))
    struct.pack_into("<H", body, 0, udf.TAG_FILE_ID)
    body[18], body[19] = flags, len(raw)
    struct.pack_into("<I", body, 24, icb)
    body[38:] = raw
    return bytes(body) + b"\0" * (-len(body) % 4)


def tiny_dvd() -> dict[int, bytes]:
    """A minimal UDF disc: VIDEO_TS holding a VOB (short_ad entry) and an IFO (extended entry, long_ad)."""
    root_dir = fid("", 1, udf.FID_PARENT | udf.FID_DIRECTORY) + fid("VIDEO_TS", 3, udf.FID_DIRECTORY)
    video_ts = (fid("", 1, udf.FID_PARENT | udf.FID_DIRECTORY) + fid("VTS_01_1.VOB", 5)
                + fid("VIDEO_TS.IFO", 6, wide=True) + fid("OLD.VOB", 5, udf.FID_DELETED))
    return {
        udf.ANCHOR_SECTOR: sector(udf.TAG_ANCHOR, {16: struct.pack("<II", 3 * udf.SECTOR, 32)}),
        32: sector(udf.TAG_PARTITION, {188: struct.pack("<I", PARTITION)}),
        33: sector(udf.TAG_LOGICAL_VOLUME, {252: struct.pack("<I", 0)}),
        34: sector(udf.TAG_TERMINATOR, {}),
        PARTITION: sector(udf.TAG_FILE_SET, {404: struct.pack("<I", 1)}),
        PARTITION + 1: file_entry(2, len(root_dir)),
        PARTITION + 2: root_dir + b"\0" * (udf.SECTOR - len(root_dir)),
        PARTITION + 3: file_entry(4, len(video_ts)),
        PARTITION + 4: video_ts + b"\0" * (udf.SECTOR - len(video_ts)),
        PARTITION + 5: file_entry(1000, 1000 * udf.SECTOR),
        PARTITION + 6: file_entry(50, 16384, tag=udf.TAG_EXTENDED_FILE_ENTRY, long_ad=True),
    }


class UdfTests(unittest.TestCase):
    def setUp(self):
        sectors = tiny_dvd()
        self.fs = udf.Udf(lambda first, count: b"".join(sectors.get(first + i, bytes(udf.SECTOR))
                                                        for i in range(count)))

    def test_finds_file_start_sectors(self):
        self.assertEqual(self.fs.file_sector("VIDEO_TS/VTS_01_1.VOB"), PARTITION + 1000)
        self.assertEqual(self.fs.file_sector("video_ts/video_ts.ifo"), PARTITION + 50)

    def test_missing_and_deleted_files(self):
        self.assertIsNone(self.fs.file_sector("VIDEO_TS/VTS_02_1.VOB"))
        self.assertIsNone(self.fs.file_sector("VIDEO_TS/OLD.VOB"))
        self.assertIsNone(self.fs.file_sector("VIDEO_TS/VTS_01_1.VOB/X"))

    def test_not_udf(self):
        with self.assertRaises(udf.UdfError):
            udf.Udf(lambda first, count: bytes(udf.SECTOR * count))


if __name__ == "__main__":
    unittest.main()
