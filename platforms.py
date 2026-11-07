"""What differs between Windows, macOS and Linux: where settings live, where the tools are,
and how to tell whether the drive has a disc, find its files and eject it.

A drive is named by the device MakeMKV reports for it: "E:" on Windows, "/dev/sr0" on
Linux, "/dev/rdisk4" (or similar) on macOS.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "GongoAutoRipper"
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

CREATE_NO_WINDOW = 0x08000000 if WINDOWS else 0
EXE = ".exe" if WINDOWS else ""

# Where the app's own files are: next to the scripts, or unpacked from a frozen build.
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TOOLS_DIR = APP_DIR / "tools"   # ffmpeg and MKVToolNix shipped with a release build

UI_FONT = "Segoe UI" if WINDOWS else "TkDefaultFont"
MONO_FONT = "Consolas" if WINDOWS else "TkFixedFont"


def data_dir(app_name: str) -> Path:
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home()) / app_name
    if MAC:
        return Path.home() / "Library" / "Application Support" / app_name
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / app_name


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                              timeout=15, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# Finding the programs
# ---------------------------------------------------------------------------

# Apps started from the macOS Finder or a Linux desktop don't always get the shell's PATH.
EXTRA_BIN_DIRS = [] if WINDOWS else ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/snap/bin"]


def _first(candidates) -> str:
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return ""


def _which(name: str) -> str:
    return shutil.which(name) or _first(Path(d) / name for d in EXTRA_BIN_DIRS)


def _newest(base: Path, pattern: str) -> str:
    matches = sorted(base.glob(pattern)) if base.is_dir() else []
    return str(matches[-1]) if matches else ""


def find_makemkvcon() -> str:
    if WINDOWS:
        found = _first(Path(base) / exe for base in (r"C:\Program Files (x86)\MakeMKV", r"C:\Program Files\MakeMKV")
                       for exe in ("makemkvcon64.exe", "makemkvcon.exe"))
        return found or shutil.which("makemkvcon64") or shutil.which("makemkvcon") or ""
    if MAC:
        app = "MakeMKV.app/Contents/MacOS/makemkvcon"
        found = _first([Path("/Applications") / app, Path.home() / "Applications" / app])
        if found:
            return found
    return _which("makemkvcon")


def find_mkvmerge() -> str:
    bundled = TOOLS_DIR / "mkvtoolnix" / f"mkvmerge{EXE}"
    if bundled.is_file():
        return str(bundled)
    if WINDOWS:
        return _first(Path(base) / "mkvmerge.exe" for base in
                      (r"C:\Program Files\MKVToolNix", r"C:\Program Files (x86)\MKVToolNix")) or _which("mkvmerge")
    if MAC:
        # The MKVToolNix app keeps its command-line tools inside the bundle.
        found = _newest(Path("/Applications"), "MKVToolNix*.app/Contents/MacOS/mkvmerge")
        if found:
            return found
    return _which("mkvmerge")


def find_ffmpeg() -> str:
    bundled = TOOLS_DIR / "ffmpeg" / f"ffmpeg{EXE}"
    if bundled.is_file():
        return str(bundled)
    if found := _which("ffmpeg"):
        return found
    if WINDOWS:
        home = Path.home()
        for base, pattern in ((Path(r"C:\Program Files"), "ffmpeg*/bin/ffmpeg.exe"),
                              (Path(r"C:\ffmpeg"), "bin/ffmpeg.exe"),
                              (home / "AppData/Local/Microsoft/WinGet/Packages", "Gyan.FFmpeg*/*/bin/ffmpeg.exe")):
            if found := _newest(base, pattern):
                return found
    return ""


# How to install libdvdcss where VideoLAN offers no ready-made download.
LIBDVDCSS_HINT = (
    "brew install libdvdcss" if MAC else
    "on Debian or Ubuntu, sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg; "
    "on Fedora, sudo dnf install libdvdcss (from RPM Fusion); on Arch, sudo pacman -S libdvdcss")


def user_lib_dir() -> Path:
    """Where the app installs libdvdcss when the user asks it to: writable, unlike the app's own folder."""
    return data_dir(APP_NAME) / "libdvdcss"


def find_libdvdcss() -> str:
    names = {"win32": ["libdvdcss-2.dll"], "darwin": ["libdvdcss.2.dylib", "libdvdcss.dylib"]}.get(
        sys.platform, ["libdvdcss.so.2", "libdvdcss.so"])
    folders = [user_lib_dir(), APP_DIR / "vendor" / "libdvdcss", APP_DIR]
    if getattr(sys, "frozen", False):
        folders.append(Path(sys.executable).parent)   # next to GongoAutoRipper.exe in a release build
    if MAC:
        folders += [Path("/opt/homebrew/lib"), Path("/usr/local/lib")]
    if found := _first(folder / name for folder in folders for name in names):
        return found
    return "" if WINDOWS else (ctypes.util.find_library("dvdcss") or "")


# ---------------------------------------------------------------------------
# The drive
# ---------------------------------------------------------------------------

if WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.SetErrorMode(0x0001 | 0x8000)  # no "insert a disk" popups for empty drives
    _k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _mac_disk(device: str) -> str:
    """/dev/rdisk4s0 -> /dev/disk4, the name `mount` and diskutil use."""
    m = re.match(r"r?(disk\d+)", Path(device).name)
    return f"/dev/{m[1]}" if m else device


def disc_root(device: str) -> Path | None:
    """The folder the disc's files are mounted at, or None if it isn't mounted."""
    if WINDOWS:
        return Path(device.rstrip("\\") + "\\")
    if LINUX:
        try:
            real = os.path.realpath(device)
            with open("/proc/self/mounts", encoding="utf-8") as f:
                for line in f:
                    source, target = line.split()[:2]
                    if os.path.realpath(source) == real:
                        # Spaces and the like in mount points are written as octal escapes (\040).
                        return Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), target))
        except (OSError, ValueError):
            pass
        return None
    if MAC:
        disk = _mac_disk(device)
        for line in _run(["mount"]).splitlines():
            source, _, rest = line.partition(" on ")
            if source == disk or source.startswith(disk + "s"):
                return Path(rest.rsplit(" (", 1)[0])
        # MakeMKV didn't give a /dev path: use the only mounted volume that holds a video disc.
        if not device.startswith("/dev/"):
            volumes = [v for v in Path("/Volumes").glob("*") if (v / "VIDEO_TS").is_dir() or (v / "BDMV").is_dir()]
            if len(volumes) == 1:
                return volumes[0]
    return None


def css_device(device: str) -> str:
    """The name libdvdcss opens the drive by. macOS needs the raw disk, /dev/rdiskN."""
    if not MAC:
        return device
    disk = _mac_disk(device) if device.startswith("/dev/") else ""
    if not disk and (root := disc_root(device)):
        for line in _run(["mount"]).splitlines():
            source, _, rest = line.partition(" on ")
            if rest.rsplit(" (", 1)[0] == str(root):
                disk = _mac_disk(source)
                break
    return disk.replace("/dev/disk", "/dev/rdisk") if disk else device


def disc_in_drive(device: str) -> tuple[int, str] | None:
    """(an id, the volume label) for the disc in the drive, or None if it's empty."""
    if WINDOWS:
        label = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        ok = _k32.GetVolumeInformationW(device.rstrip("\\") + "\\", label, len(label), ctypes.byref(serial),
                                        None, None, None, 0)
        return (serial.value, label.value) if ok else None
    if LINUX:
        import fcntl
        CDROM_DRIVE_STATUS, CDSL_CURRENT, CDS_DISC_OK = 0x5326, 0x7FFFFFFF, 4
        try:
            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            if fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT) != CDS_DISC_OK:
                return None
        except OSError:
            return None
        finally:
            os.close(fd)
    elif MAC:
        status = _run(["drutil", "status"])
        if not status or "No Media" in status:
            return None
    root = disc_root(device)
    return 0, root.name if root else ""


def eject_disc(device: str) -> bool:
    if WINDOWS:
        letter = device.rstrip("\\")
        GENERIC_READ, SHARE_READ_WRITE, OPEN_EXISTING = 0x80000000, 0x3, 3
        IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
        handle = _k32.CreateFileW(f"\\\\.\\{letter}", GENERIC_READ, SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                returned = wintypes.DWORD()
                if _k32.DeviceIoControl(handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
                                        ctypes.byref(returned), None):
                    return True
            finally:
                _k32.CloseHandle(handle)
        # Fall back to Explorer's Eject verb.
        script = f"(New-Object -ComObject Shell.Application).Namespace(17).ParseName('{letter}').InvokeVerb('Eject')"
        cmd = ["powershell", "-NoProfile", "-Command", script]
    elif MAC:
        cmd = ["drutil", "eject"] if not device.startswith("/dev/") else ["diskutil", "eject", _mac_disk(device)]
    else:
        cmd = ["eject", device]
    try:
        return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
                              creationflags=CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def makemkv_gui_running() -> bool:
    if WINDOWS:
        return "makemkv.exe" in _run(["tasklist", "/FI", "IMAGENAME eq makemkv.exe", "/NH"]).lower()
    return bool(_run(["pgrep", "-x", "MakeMKV" if MAC else "makemkv"]).strip())
