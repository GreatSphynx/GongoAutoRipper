"""Build a standalone GongoAutoRipper for the system this runs on.

    python build.py --tools --version 1.0.0

--tools downloads the programs bundled with the build into tools/ first:
  Windows: ffmpeg, MKVToolNix (mkvmerge, mkvextract)
  macOS:   ffmpeg
  Linux:   nothing; ffmpeg and MKVToolNix come from the system's packages
MakeMKV is never bundled: it has its own licence and installer. Nor is libdvdcss, which
only adds previews of CSS-protected DVDs; users who want them download it themselves.

Needs: pip install pyinstaller -r requirements.txt, and 7-Zip on Windows
Output in dist/, .tar.gz on Linux and .zip elsewhere:
  GongoAutoRipper-<version>-<system>-full   with the downloaded programs
  GongoAutoRipper-<version>-<system>-lite   without them, for people who point the app at their own
Without downloaded programs (Linux, or no --tools) there is one package, with no suffix.
"""

from __future__ import annotations

import argparse
import io
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
DIST = HERE / "dist"
NAME = "GongoAutoRipper"

FFMPEG_WINDOWS = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
FFMPEG_MAC = "https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/{tool}.zip"
MKVTOOLNIX_LATEST = "https://mkvtoolnix.download/latest-release.xml"
MKVTOOLNIX_WINDOWS = "https://mkvtoolnix.download/windows/releases/{v}/mkvtoolnix-64-bit-{v}.7z"


def system() -> str:
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux") + "-" + arch


def download(url: str) -> bytes:
    print(f"  {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"{NAME}-build"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def fetch_tools() -> None:
    shutil.rmtree(TOOLS, ignore_errors=True)
    if sys.platform == "win32":
        ffmpeg = TOOLS / "ffmpeg"
        ffmpeg.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(download(FFMPEG_WINDOWS))) as z:
            for member in z.namelist():
                if re.search(r"/bin/(ffmpeg\.exe|ffprobe\.exe|[^/]+\.dll)$", member):
                    (ffmpeg / Path(member).name).write_bytes(z.read(member))
        # The MKVToolNix archive uses a 7z filter (BCJ2) that only 7-Zip itself unpacks.
        seven = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
        version = re.search(r"<version>([\d.]+)</version>", download(MKVTOOLNIX_LATEST).decode())[1]
        archive = TOOLS / "mkvtoolnix.7z"
        archive.write_bytes(download(MKVTOOLNIX_WINDOWS.format(v=version)))
        subprocess.run([seven, "e", "-y", "-r", f"-o{TOOLS / 'mkvtoolnix'}", str(archive),
                        "mkvmerge.exe", "mkvextract.exe"], check=True, stdout=subprocess.DEVNULL)
        archive.unlink()
    elif sys.platform == "darwin":
        ffmpeg = TOOLS / "ffmpeg"
        ffmpeg.mkdir(parents=True)
        arch = "arm64" if system().endswith("arm64") else "amd64"
        for tool in ("ffmpeg", "ffprobe"):
            with zipfile.ZipFile(io.BytesIO(download(FFMPEG_MAC.format(arch=arch, tool=tool)))) as z:
                (ffmpeg / tool).write_bytes(z.read(tool))
            (ffmpeg / tool).chmod(0o755)


def pyinstaller(with_tools: bool) -> None:
    sep = ";" if sys.platform == "win32" else ":"
    icon = HERE / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", NAME,
           "--icon", str(icon), "--add-data", f"{HERE / 'assets'}{sep}assets"]
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "io.github.greatsphynx.gongoautoripper"]
    # macOS needs the tools added as binaries so they end up signed. On Windows they're copied
    # in afterwards: PyInstaller would otherwise also put a second copy of ffmpeg's DLLs next to the app.
    if with_tools and sys.platform != "win32":
        for tool in sorted(TOOLS.rglob("*")):
            if tool.is_file():
                cmd += ["--add-binary", f"{tool}{sep}{tool.parent.relative_to(HERE).as_posix()}"]
    cmd.append(str(HERE / "gongoautoripper.py"))
    subprocess.run(cmd, check=True, cwd=HERE)


def package(name: str) -> Path:
    notices = [HERE / "LICENSE.md", HERE / "THIRD-PARTY.md"]
    if sys.platform == "darwin":
        out = DIST / f"{name}.zip"
        # ditto keeps the app bundle's symlinks and permissions, which zipfile doesn't.
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(DIST / f"{NAME}.app"), str(out)], check=True)
        with zipfile.ZipFile(out, "a") as z:
            for notice in notices:
                z.write(notice, notice.name)
        return out
    folder = DIST / NAME
    for notice in notices:
        shutil.copy(notice, folder / notice.name)
    if sys.platform == "win32":
        out = DIST / f"{name}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(folder.rglob("*")):
                z.write(path, Path(NAME) / path.relative_to(folder))
    else:
        out = DIST / f"{name}.tar.gz"
        with tarfile.open(out, "w:gz") as t:
            t.add(folder, NAME)
    return out


def build(version: str) -> list[Path]:
    """A "lite" package without the downloaded tools and a "full" one with them, or a
    single package when there are no tools for this system."""
    shutil.rmtree(HERE / "build", ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    base = f"{NAME}-{version}-{system()}"
    has_tools = TOOLS.is_dir() and any(p.is_file() for p in TOOLS.rglob("*"))
    pyinstaller(with_tools=False)
    if not has_tools:
        return [package(base)]
    outputs = [package(f"{base}-lite")]
    if sys.platform == "win32":
        shutil.copytree(TOOLS, DIST / NAME / "_internal" / "tools")
    else:
        pyinstaller(with_tools=True)
    outputs.append(package(f"{base}-full"))
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tools", action="store_true", help="download the bundled programs first")
    parser.add_argument("--version", default="dev")
    args = parser.parse_args()
    if args.tools:
        print("Downloading bundled programs:")
        fetch_tools()
    for out in build(args.version.lstrip("v")):
        print(f"Built {out}")


if __name__ == "__main__":
    main()
