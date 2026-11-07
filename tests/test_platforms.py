import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platforms  # noqa: E402


class PlatformTests(unittest.TestCase):
    def test_mac_device_names(self):
        self.assertEqual(platforms._mac_disk("/dev/rdisk4"), "/dev/disk4")
        self.assertEqual(platforms._mac_disk("/dev/disk12s1"), "/dev/disk12")
        self.assertEqual(platforms._mac_disk("IOBDServices"), "IOBDServices")

    def test_data_dir_per_system(self):
        with mock.patch.multiple(platforms, WINDOWS=False, MAC=True, LINUX=False):
            self.assertEqual(platforms.data_dir("X").parts[-3:], ("Library", "Application Support", "X"))
        with mock.patch.multiple(platforms, WINDOWS=False, MAC=False, LINUX=True), \
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "/cfg"}):
            self.assertEqual(platforms.data_dir("X"), Path("/cfg/X"))

    def test_mac_mount_lookup(self):
        mounts = "/dev/disk1s1 on / (apfs, local)\n/dev/disk4 on /Volumes/GREAT CHEFS 4 (udf, local, nodev)\n"
        with mock.patch.multiple(platforms, WINDOWS=False, MAC=True, LINUX=False), \
                mock.patch.object(platforms, "_run", return_value=mounts):
            self.assertEqual(platforms.disc_root("/dev/rdisk4"), Path("/Volumes/GREAT CHEFS 4"))
            self.assertIsNone(platforms.disc_root("/dev/rdisk5"))


if __name__ == "__main__":
    unittest.main()
