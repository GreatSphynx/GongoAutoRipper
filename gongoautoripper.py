"""GongoAutoRipper: drive MakeMKV to rip a run of discs with automatic disc/episode
file naming, e.g. "Great Chefs Disc 2 Episode 5.mkv", or rip each disc as one
file in its Play All order with the bonus titles on the end.

Run with:  pythonw gongoautoripper.py
"""

from __future__ import annotations

import csv
import ctypes
import dataclasses
import json
import os
import queue
import re
import shutil
import string
import subprocess
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import dvdnav
import lookup
import platforms
import thumbs
from dvdnav import DiscOrder
from platforms import (CREATE_NO_WINDOW, MONO_FONT, UI_FONT, disc_in_drive, disc_root, eject_disc, find_ffmpeg,
                       find_makemkvcon, find_mkvmerge, makemkv_gui_running)

try:
    from PIL import Image, ImageTk
except ImportError:  # previews are optional
    Image = ImageTk = None

MISSING_FOR_PREVIEWS = [name for name, module in (("Pillow", Image), ("opencv-python", thumbs.cv2)) if module is None]

APP_NAME = platforms.APP_NAME
DATA_DIR = platforms.data_dir(APP_NAME)
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "gongoautoripper.log"
PREVIEW_DIR = DATA_DIR / "previews"
ICON_DIR = platforms.APP_DIR / "assets"
PROGRAM_NAMES = {"makemkvcon": "makemkvcon64.exe", "mkvmerge": "mkvmerge.exe", "ffmpeg": "ffmpeg.exe"} \
    if platforms.WINDOWS else {"makemkvcon": "makemkvcon", "mkvmerge": "mkvmerge", "ffmpeg": "ffmpeg"}
DEFAULT_TEMPLATE = "{show} Disc {disc} Episode {episode}"
DEFAULT_WHOLE_TEMPLATE = "{show} Disc {disc}"
TMP_DIR_NAME = ".gongoautoripper-tmp"
EPISODES, WHOLE = "episodes", "whole"
BONUS_MIN_SECONDS = 30
# Copy speed assumed when choosing between one MakeMKV run and separate rips. Deliberately a
# slow disc's (protected discs have copied at ~3.5 MB/s, others at ~7): ripping in one pass is
# only chosen when it wins even then, because copying unneeded titles is the costly mistake.
DISC_READ_BYTES_PER_SECOND = 3_000_000

# MakeMKV robot-mode (-r) attribute ids, from apdefs.h in the MakeMKV SDK.
ATTR_CHAPTERS = 8
ATTR_DURATION = 9
ATTR_SIZE = 10
ATTR_SIZE_BYTES = 11
ATTR_SOURCE = 16
ATTR_ORIGINAL_TITLE = 24
ATTR_SEGMENTS = 26
ATTR_OUTPUT_FILE = 27
CINFO_NAME = 2
MSG_TOO_OLD = 5021
# Per-title scan chatter ("Title #12 was added", "Cells 2-2 were removed...") kept out of the log.
QUIET_MSG_CODES = {3025, 3028, 3038}


# ---------------------------------------------------------------------------
# MakeMKV output parsing
# ---------------------------------------------------------------------------

@dataclass
class Drive:
    index: int
    device: str   # as MakeMKV names it: "E:", "/dev/sr0"...
    name: str
    disc_name: str

    @property
    def label(self) -> str:
        return f"{self.device}  {self.name}"


@dataclass
class Title:
    index: int
    duration: int = 0
    size: str = ""
    size_bytes: int = 0
    chapters: int = 0
    source: str = ""
    segments: str = ""
    output_file: str = ""
    dvd_title: int = 0
    role: str = ""          # playall / container / inside / duplicate / intro / bonus, or "" if unknown
    position: int = 0       # place in Play All
    selected: bool = True
    skip_reason: str = ""
    kind: str = ""          # episode / bonus / part
    episode: int | None = None
    part: int = 0
    filename: str = ""
    status: str = ""
    warning: str = ""
    # From TheDiscDB, when the disc is in its database:
    online_type: str = ""       # Episode, MainMovie, Extra, Trailer, DeletedScene...
    online_season: str = ""
    online_episode: str = ""
    online_name: str = ""       # episode name or extra's description

    @property
    def part_name(self) -> str:
        label = {"bonus": "Bonus", "full_bonus": "Bonus", "intro": "Intro"}.get(self.role, "Part")
        return f"{label} {self.part:02}.mkv"


@dataclass
class DiscInfo:
    name: str = ""
    titles: list[Title] = dataclasses.field(default_factory=list)
    messages: list[tuple[int, str]] = dataclasses.field(default_factory=list)


class FatalError(Exception):
    pass


class Stopped(Exception):
    pass


def parse_robot_line(line: str) -> tuple[str, list[str]]:
    """Split a robot-mode line like 'TINFO:0,9,0,"0:44:12"' into kind and fields."""
    kind, sep, rest = line.partition(":")
    if not sep or not kind.isalpha() or not kind.isupper():
        return "", []
    try:
        return kind, next(csv.reader([rest]))
    except (csv.Error, StopIteration):
        return kind, []


def parse_duration(text: str) -> int:
    total = 0
    for part in text.split(":"):
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}"


def parse_drives(lines: list[str]) -> list[Drive]:
    drives = []
    for line in lines:
        kind, f = parse_robot_line(line)
        if kind == "DRV" and len(f) >= 7 and f[6]:
            drives.append(Drive(int(f[0]), f[6].rstrip("\\"), f[4], f[5]))
    return drives


def parse_info(lines: list[str]) -> DiscInfo:
    info = DiscInfo()
    titles: dict[int, Title] = {}
    for line in lines:
        kind, f = parse_robot_line(line)
        if kind == "CINFO" and len(f) >= 3 and f[0] == str(CINFO_NAME):
            info.name = f[2]
        elif kind == "TINFO" and len(f) >= 4 and f[0].isdigit() and f[1].isdigit():
            t = titles.setdefault(int(f[0]), Title(int(f[0])))
            attr, value = int(f[1]), f[3]
            if attr == ATTR_DURATION:
                t.duration = parse_duration(value)
            elif attr == ATTR_SIZE:
                t.size = value
            elif attr == ATTR_SIZE_BYTES:
                t.size_bytes = int(value) if value.isdigit() else 0
            elif attr == ATTR_CHAPTERS:
                t.chapters = int(value) if value.isdigit() else 0
            elif attr == ATTR_SOURCE:
                t.source = value
            elif attr == ATTR_ORIGINAL_TITLE:
                t.dvd_title = int(value) if value.isdigit() else 0
            elif attr == ATTR_SEGMENTS:
                t.segments = value
            elif attr == ATTR_OUTPUT_FILE:
                t.output_file = value
        elif kind == "MSG" and len(f) >= 4 and f[0].isdigit():
            info.messages.append((int(f[0]), f[3]))
    info.titles = [titles[i] for i in sorted(titles)]
    return info


def license_error(messages: list[tuple[int, str]]) -> str | None:
    for code, text in messages:
        lowered = text.lower()
        if code == MSG_TOO_OLD or "registration key" in lowered or "too old" in lowered:
            return text
    return None


# ---------------------------------------------------------------------------
# Choosing, ordering and naming titles
# ---------------------------------------------------------------------------

@dataclass
class PlanSettings:
    mode: str = EPISODES
    min_minutes: float = 10
    max_minutes: float = 0
    skip_duplicates: bool = True
    skip_play_all: bool = True
    include_bonus: bool = True


ROLE_LABELS = {"playall": "Play All {}", "container": "Play All title", "inside": "In Play All",
               "duplicate": "Duplicate", "intro": "Intro", "bonus": "Bonus - not in Play All",
               "full": "Whole disc {}", "full_bonus": "Whole disc {} (extra)", "covered": "Already included"}


def order_note(order: DiscOrder | None, note: str, mode: str) -> str:
    """What the table's order line should say for this disc in this mode."""
    if mode == WHOLE and order is not None and order.complete:
        return order.complete_note
    return note


def role_label(t: Title) -> str:
    if t.role == "online_episode":
        return f"S{t.online_season}E{t.online_episode} (TheDiscDB)"
    if t.role == "bonus" and t.online_type:
        return f"{t.online_type} (TheDiscDB)"
    return ROLE_LABELS.get(t.role, "").format(t.position)


def _skip(t: Title, reason: str) -> None:
    t.selected, t.skip_reason = False, reason


def _skip_duplicates(titles: list[Title]) -> None:
    # DVD segment maps are numbered per title ("1", "1-2"...), so on their own they
    # don't identify content; a true duplicate also has the exact same byte size.
    seen: dict[tuple, int] = {}
    for t in titles:
        if not t.selected or not t.segments or not t.size_bytes:
            continue
        key = (t.segments, t.duration, t.size_bytes)
        if key in seen:
            _skip(t, f"duplicate of title {seen[key]}")
        else:
            seen[key] = t.index


def _play_all_title(titles: list[Title]) -> Title | None:
    """A title roughly as long as all the other selected titles combined."""
    kept = [t for t in titles if t.selected]
    if len(kept) < 3:
        return None
    longest = max(kept, key=lambda t: t.duration)
    rest = sum(t.duration for t in kept if t is not longest)
    return longest if rest and abs(longest.duration - rest) <= max(60, 0.03 * rest) else None


def apply_filters(titles: list[Title], min_minutes: float, max_minutes: float,
                  skip_duplicates: bool, skip_play_all: bool) -> None:
    """Pre-select the titles that look like episodes, in disc order."""
    for t in titles:
        t.selected, t.skip_reason = True, ""
        if t.duration < min_minutes * 60:
            _skip(t, f"shorter than {min_minutes:g} min")
        elif max_minutes > 0 and t.duration > max_minutes * 60:
            _skip(t, f"longer than {max_minutes:g} min")
    if skip_duplicates:
        _skip_duplicates(titles)
    if skip_play_all and (longest := _play_all_title(titles)):
        _skip(longest, "looks like a 'play all' title")


def classify(titles: list[Title], order: DiscOrder) -> None:
    rank = {number: i for i, number in enumerate(order.playall, start=1)}
    for t in titles:
        n = t.dvd_title
        t.position = rank.get(n, order.inside.get(n, 0))
        if n in order.containers:
            t.role = "container"
        elif n in rank:
            t.role = "playall"
        elif n in order.inside:
            t.role = "inside"
        elif n in order.duplicates:
            t.role = "duplicate"
        elif n in order.intro:
            t.role = "intro"
        else:
            t.role = "bonus"


def _plan_whole_disc(titles: list[Title], order: DiscOrder, s: PlanSettings) -> list[Title]:
    """Tick the titles that between them play everything on the disc once, in order."""
    rank = {n: i for i, n in enumerate(order.complete, start=1)}
    for t in titles:
        n = t.dvd_title
        if n in rank:
            t.role, t.position = ("full_bonus" if n in order.complete_extras else "full"), rank[n]
            if n in order.repeats:
                t.warning = f"also repeats {fmt_duration(int(order.repeats[n]))} of video already included"
            if n in order.complete_extras and not s.include_bonus:
                _skip(t, "extras are turned off")
        elif n in order.intro:
            t.role = "intro"
            _skip(t, "plays before the menu")
        else:
            t.role = "covered"
            _skip(t, order.left_out.get(n, "its video is already included"))
    group = {"intro": 0, "full": 1, "full_bonus": 1}
    return sorted(titles, key=lambda t: (group.get(t.role, 2), t.position, t.dvd_title or t.index, t.index))


def _number(text: str) -> int:
    """The first number in a season/episode label like "11-12", for sorting."""
    m = re.match(r"\d+", text or "")
    return int(m[0]) if m else 0


def _plan_from_discdb(titles: list[Title], s: PlanSettings) -> list[Title]:
    """Episodes as TheDiscDB labels them, in season/episode order, then the labelled extras."""
    for t in titles:
        if t.online_type in ("Episode", "MainMovie"):
            t.role = "online_episode"
        elif t.online_type:
            t.role = "bonus"
            if not s.include_bonus:
                _skip(t, "extras are turned off")
        else:
            _skip(t, "not labelled on TheDiscDB")

    def rank(t: Title):
        group = {"online_episode": 0, "bonus": 1}.get(t.role, 2)
        return (group, _number(t.online_season), _number(t.online_episode), t.dvd_title or t.index, t.index)

    return sorted(titles, key=rank)


def build_plan(titles: list[Title], order: DiscOrder | None, s: PlanSettings) -> list[Title]:
    """Choose which titles to rip and put them in rip order (unchosen ones go last)."""
    for t in titles:
        t.selected, t.skip_reason, t.role, t.position, t.warning = True, "", "", 0, ""

    # A disc TheDiscDB knows has its episodes labelled exactly; that beats any guess.
    if s.mode == EPISODES and any(t.online_type for t in titles):
        return _plan_from_discdb(titles, s)
    if s.mode == WHOLE and order is not None and order.complete:
        return _plan_whole_disc(titles, order, s)
    if order is not None and not order.playall:
        order = None  # no Play All on the menus: episodes go in disc order

    if order is None:
        if s.mode == EPISODES:
            apply_filters(titles, s.min_minutes, s.max_minutes, s.skip_duplicates, s.skip_play_all)
        else:
            for t in titles:
                if t.duration < BONUS_MIN_SECONDS:
                    _skip(t, f"under {BONUS_MIN_SECONDS} s")
            if s.skip_duplicates:
                _skip_duplicates(titles)
            if longest := _play_all_title(titles):
                for t in titles:
                    if t.selected and t is not longest:
                        _skip(t, "probably inside the 'play all' title")
        return sorted(titles, key=lambda t: (not t.selected, t.index))

    classify(titles, order)
    for t in titles:
        if t.role == "intro":
            _skip(t, "plays before the menu")
        elif t.role == "duplicate":
            _skip(t, "repeats video that's in Play All")
        elif t.role == "bonus":
            if not s.include_bonus:
                _skip(t, "extras are turned off")
            elif t.duration < BONUS_MIN_SECONDS:
                _skip(t, f"bonus title under {BONUS_MIN_SECONDS} s")
        elif s.mode == WHOLE:
            if t.role == "inside":
                _skip(t, "already inside the Play All title")
        elif t.role == "container":
            _skip(t, "Play All title; its episodes are ripped separately")
        elif t.duration < s.min_minutes * 60:
            _skip(t, f"shorter than {s.min_minutes:g} min")
        elif s.max_minutes > 0 and t.duration > s.max_minutes * 60:
            _skip(t, f"longer than {s.max_minutes:g} min")
    if s.skip_duplicates:
        _skip_duplicates([t for t in titles if t.role == "bonus"])

    # Pre-menu titles go first (unticked), so ticking one puts it where a player shows it.
    def rank(t: Title):
        if t.role in ("playall", "container", "inside"):
            return (1, t.position, t.index)
        group = {"intro": 0, "bonus": 2}.get(t.role, 3)
        return (group, t.dvd_title or t.index, t.index)

    return sorted(titles, key=rank)


# Why titles are left out, grouped for the summary under the review buttons.
DROP_REASONS = (
    (r"repeats video|already inside|duplicate of|inside the 'play all'|Play All title;|looks like a 'play all'",
     "video already in the ticked titles"),
    (r"shorter than|under \d+ s|under a second", "shorter than the minimum length"),
    (r"longer than", "longer than the maximum length"),
    (r"before the menu", "plays before the menu"),
    (r"extras are turned off", "extras, which are turned off"),
    (r"not labelled on TheDiscDB", "not labelled on TheDiscDB"),
)


def rip_summary(titles: list[Title], mode: str) -> tuple[str, str]:
    """Two lines saying what Rip selected will produce and which titles are left out, and why."""
    chosen = [t for t in titles if t.selected]
    if not chosen:
        made = "Nothing is ticked, so nothing will be ripped from this disc."
    elif mode == WHOLE:
        made = (f"This rip will create 1 merged file from {len(chosen)} titles." if len(chosen) > 1
                else "This rip will create 1 file.")
    else:
        episodes = sum(1 for t in chosen if t.kind == "episode")
        others = len(chosen) - episodes
        parts = [f"{episodes} episode{'s' if episodes != 1 else ''}"] if episodes else []
        parts += [f"{others} bonus"] if others else []
        made = (f"This rip will create {len(chosen)} separate file{'s' if len(chosen) != 1 else ''} "
                f"({', '.join(parts)}).")
    dropped = [t for t in titles if not t.selected]
    if not dropped:
        return made, "No titles are dropped."
    counts: dict[str, int] = {}
    for t in dropped:
        reason = next((label for pattern, label in DROP_REASONS if re.search(pattern, t.skip_reason)),
                      "unticked by hand" if not t.skip_reason else t.skip_reason)
        counts[reason] = counts.get(reason, 0) + 1
    reasons = ", ".join(f"{n}× {label}" for label, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return made, f"{len(dropped)} title{'s are' if len(dropped) != 1 else ' is'} dropped: {reasons}."


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return name.strip().rstrip(". ") or "untitled"


PLACEHOLDERS = ("show", "disc", "episode", "title", "year", "season", "season_episode", "episode_title")


class _Fields(string.Formatter):
    """Template formatting where a value that isn't known (e.g. no online match) becomes empty."""

    def get_value(self, key, args, kwargs):
        return kwargs.get(key, "")

    def format_field(self, value, spec):
        if value in ("", None):
            return ""
        if isinstance(value, str) and value.isdigit():
            value = int(value)  # so {season:02} pads "1" to "01", not "10"
        try:
            return format(value, spec)
        except (ValueError, TypeError):
            try:
                return format(int(value), spec)
            except (ValueError, TypeError):
                return str(value)


def template_problem(template: str) -> str:
    """Why a file name pattern can't be used, or "" if it's fine."""
    try:
        names = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError:
        return "has an unmatched { or }"
    unknown = [n for n in names if n.split(".")[0].split("[")[0] not in PLACEHOLDERS]
    return f"uses {{{unknown[0]}}}, which isn't one of the placeholders" if unknown else ""


def format_name(template: str, show: str, disc: int, episode: int, **online) -> str:
    """Fill in a file name pattern. `online` can carry title, year, season, season_episode
    and episode_title; any that are missing leave their placeholder (and any brackets or
    dashes left dangling around it) out."""
    name = _Fields().format(template, show=show, disc=disc, episode=episode, **online)
    if not online.get("season") and not online.get("season_episode"):
        name = re.sub(r"(?<![A-Za-z0-9])SE(?![A-Za-z0-9])", "", name)  # what's left of S{season}E{season_episode}
    name = re.sub(r"\(\s*\)|\[\s*\]", "", name)
    name = re.sub(r"(\s*-\s*){2,}", " - ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return safe_filename(name) + ".mkv"


@dataclass
class OnlineNames:
    """Names found online for a show or disc: its official title/year, and episode names
    in order (episode 1 of the series first), used to fill the online placeholders."""
    title: str = ""
    year: str = ""
    episodes: list = dataclasses.field(default_factory=list)   # lookup.Episode

    def fields(self, episode: int | None = None, t: Title | None = None) -> dict:
        out = {"title": self.title, "year": self.year}
        if t is not None and t.online_type in ("Episode", "MainMovie"):
            out.update(season=t.online_season, season_episode=t.online_episode, episode_title=t.online_name)
        elif episode is not None and 0 < episode <= len(self.episodes):
            e = self.episodes[episode - 1]
            out.update(season=e.season, season_episode=e.number, episode_title=e.title)
        return out


def assign_names(titles: list[Title], mode: str, template: str, whole_template: str, show: str,
                 disc: int, first_episode: int, names: OnlineNames | None = None) -> int:
    """Name the selected titles in list order; returns the next free episode number."""
    names = names or OnlineNames()
    whole = format_name(whole_template, show, disc, first_episode, **names.fields())
    episode = first_episode
    part = bonus = intro = 0
    for t in titles:
        t.kind, t.episode, t.part, t.filename = "", None, 0, ""
        if not t.selected:
            continue
        if mode == WHOLE:
            part += 1
            t.kind, t.part, t.filename = "part", part, whole
        elif t.role == "bonus":
            bonus += 1
            extra = f" - {safe_filename(t.online_name)}" if t.online_name else ""
            t.kind, t.filename = "bonus", f"{whole[:-4]} Bonus {bonus}{extra}.mkv"
        elif t.role == "intro":
            intro += 1
            t.kind, t.filename = "bonus", f"{whole[:-4]} Intro {intro}.mkv"
        else:
            t.kind, t.episode = "episode", episode
            t.filename = format_name(template, show, disc, episode, **names.fields(episode, t))
            episode += 1
    return episode


def rip_in_one_run(pending: list[Title], all_titles: list[Title], scan_seconds: float,
                   bytes_per_second: float = DISC_READ_BYTES_PER_SECOND) -> bool:
    """MakeMKV re-scans the whole disc every time it starts. Ripping every title in one run
    scans once but also copies the titles we don't need; pick whichever should be quicker."""
    if len(pending) < 2:
        return False
    wanted = {t.index for t in pending}
    unneeded = sum(t.size_bytes for t in all_titles if t.index not in wanted)
    return unneeded / bytes_per_second < (len(pending) - 1) * scan_seconds


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# makemkvcon, mkvmerge and the optical drive
# ---------------------------------------------------------------------------

class MakeMKV:
    """Runs makemkvcon in robot mode."""

    def __init__(self, command: list[str]):
        self.command = command
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def run(self, args: list[str], on_line=None, keep: bool = True) -> tuple[int, list[str]]:
        proc = subprocess.Popen(
            [*self.command, "-r", *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        with self._lock:
            self._proc = proc
        lines: list[str] = []
        try:
            with proc:
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    if keep:
                        lines.append(line)
                    if on_line:
                        on_line(line)
            return proc.returncode, lines
        finally:
            with self._lock:
                self._proc = None

    def kill(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def drives(self) -> tuple[list[Drive], str | None]:
        """The drives MakeMKV can see, plus its licence complaint if it refused to run."""
        lines = self.run(["--cache=1", "info", "disc:9999"])[1]
        return parse_drives(lines), license_error(parse_info(lines).messages)

    # Titles are always listed with no minimum length, so that title numbers stay
    # the same between scanning and ripping and short Play All pieces are included.
    def info(self, disc: int) -> DiscInfo:
        return parse_info(self.run(["--cache=1", "--minlength=0", "info", f"disc:{disc}"])[1])

    def rip(self, disc: int, title: int | str, out_dir: Path, on_line) -> int:
        """Rip one title, or every title with title="all"."""
        args = ["--progress=-same", "--minlength=0", "mkv", f"disc:{disc}", str(title), str(out_dir)]
        return self.run(args, on_line, keep=False)[0]


def _run_text(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                          creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8-sig", errors="replace")


def chapter_layout(command: list[str], parts: list[Path]) -> list[tuple[float, str]] | None:
    """Chapter marks for the joined file: each part's own chapters moved to where the part
    lands, or a single mark at its start if it has none. None if MKVToolNix can't be asked."""
    mkvmerge = Path(command[0])
    mkvextract = mkvmerge.with_name("mkvextract" + mkvmerge.suffix)
    if not mkvmerge.stem.lower() == "mkvmerge" or not mkvextract.exists():
        return None
    marks: list[tuple[float, str]] = []
    offset = 0.0
    for part in parts:
        try:
            duration = json.loads(_run_text([str(mkvmerge), "-J", str(part)]))["container"]["properties"]["duration"] / 1e9
        except (ValueError, KeyError, TypeError):
            return None
        own = []
        lines = _run_text([str(mkvextract), str(part), "chapters", "--simple", "-"]).splitlines()
        for line in lines:
            if m := re.fullmatch(r"CHAPTER\d+=(\d+):(\d+):([\d.]+)", line.strip()):
                own.append([int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), ""])
            elif (m := re.fullmatch(r"CHAPTER\d+NAME=(.*)", line.strip())) and own:
                own[-1][1] = m[1]
        own = [(start, name) for start, name in own if start < duration]
        if len(own) > 1:
            marks += [(offset + start, f"{part.stem} - {name or f'Chapter {n:02}'}")
                      for n, (start, name) in enumerate(own, start=1)]
        else:
            marks.append((offset, part.stem))
        offset += duration
    return marks


def join_mkvs(command: list[str], parts: list[Path], out: Path) -> tuple[bool, str]:
    """Append MKVs into one file with mkvmerge, keeping each part's own chapter marks
    (or putting one at the start of a part that has none)."""
    cmd = [*command, "-o", str(out)]
    marks = chapter_layout(command, parts)
    if marks:
        chapters = out.with_name(f"{out.stem}.chapters.txt")
        lines = []
        for n, (start, name) in enumerate(marks, start=1):
            h, rem = divmod(start, 3600)
            m, s = divmod(rem, 60)
            lines += [f"CHAPTER{n:02}={int(h):02}:{int(m):02}:{s:06.3f}", f"CHAPTER{n:02}NAME={name}"]
        chapters.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd += ["--chapter-charset", "UTF-8", "--chapters", str(chapters)]
    else:
        cmd += ["--generate-chapters", "when-appending", "--generate-chapters-name-template", "<FILE_NAME>"]
    for i, part in enumerate(parts):
        if i:
            cmd.append("+")
        cmd += ["--no-chapters", str(part)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    # mkvmerge exits 1 for warnings, 2 for errors.
    ok = result.returncode in (0, 1) and out.exists() and out.stat().st_size > 0
    return ok, (result.stdout + result.stderr).strip()[-1500:]


# Audio codecs ffmpeg can re-encode to the same format, so padded parts still join with the rest.
PAD_ENCODERS = {"ac3": "ac3", "eac3": "eac3", "mp2": "mp2", "mp3": "libmp3lame", "aac": "aac",
                "flac": "flac", "pcm_s16le": "pcm_s16le", "pcm_s24le": "pcm_s24le"}
AUDIO_GAP_SECONDS = 0.5


def _ffprobe(ffmpeg: str, args: list[str], path: Path) -> str:
    ffprobe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    return subprocess.run([str(ffprobe), "-v", "error", *args, str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          creationflags=CREATE_NO_WINDOW).stdout


def pad_short_audio(ffmpeg: str, part: Path) -> str:
    """Pad a part's audio with silence when it stops well before the video.

    DVD title cards often carry only a second of (silent) audio under several seconds
    of picture. Inside a joined file that leaves a hole in the audio, and players that
    time video against audio skip the picture to catch up. Returns a note for the log,
    or "" when the part was fine.
    """
    info = json.loads(_ffprobe(ffmpeg, ["-show_entries", "format=duration:stream=codec_type,codec_name,bit_rate",
                                        "-of", "json"], part) or "{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio or "duration" not in info.get("format", {}):
        return ""
    duration = float(info["format"]["duration"])
    packets = _ffprobe(ffmpeg, ["-select_streams", "a:0", "-show_entries", "packet=pts_time,duration_time",
                                "-of", "csv=p=0"], part).split()
    try:
        pts, length = (float(x) for x in packets[-1].split(",")[:2])
        audio_end = pts + length
    except (IndexError, ValueError):
        audio_end = 0.0
    gap = duration - audio_end
    if gap <= AUDIO_GAP_SECONDS:
        return ""
    codec = audio[0].get("codec_name", "")
    if codec not in PAD_ENCODERS:
        return f"its audio stops {gap:.1f} s early, but {codec or 'this'} audio can't be padded"
    padded = part.with_name(f"{part.stem}.padded.mkv")
    cmd = [ffmpeg, "-v", "error", "-y", "-i", str(part), "-map", "0", "-c", "copy", "-c:a", PAD_ENCODERS[codec]]
    if str(audio[0].get("bit_rate", "")).isdigit():
        cmd += ["-b:a", audio[0]["bit_rate"]]
    cmd += ["-af", f"apad=whole_dur={duration:.3f}", str(padded)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0 or not padded.exists():
        padded.unlink(missing_ok=True)
        return f"its audio stops {gap:.1f} s early and padding failed: {result.stderr.strip()[-300:]}"
    os.replace(padded, part)
    return f"padded {gap:.1f} s of missing audio with silence"


def identify_disc(device: str) -> tuple[lookup.DiscMatch | None, str]:
    root = disc_root(device)
    if root is None:
        return None, "TheDiscDB: the disc isn't mounted, so its files can't be read"
    return lookup.identify_disc(root)


def analyze_disc_order(device: str, available: set[int] | None = None) -> tuple[DiscOrder | None, str]:
    root = disc_root(device)
    if root is None:
        return None, "disc order (the disc isn't mounted, so its DVD menus can't be read)"
    if not (root / "VIDEO_TS" / "VIDEO_TS.IFO").exists():
        return None, "disc order (not a DVD, so there are no DVD menus to read)"
    try:
        order = dvdnav.analyze(root, available)
    except Exception as e:
        return None, f"disc order (couldn't follow the DVD menus: {e})"
    if order is None:
        return None, "disc order (no Play All found in the DVD menus)"
    return order, order.note


# ---------------------------------------------------------------------------
# Rip session (runs on a worker thread, talks to the GUI through emit())
# ---------------------------------------------------------------------------

@dataclass
class SessionConfig:
    command: list[str]
    drive: Drive
    out_dir: Path
    show: str
    template: str
    disc: int
    episode: int
    min_minutes: float
    max_minutes: float
    skip_duplicates: bool
    skip_play_all: bool
    review: bool
    eject: bool
    mode: str = EPISODES
    whole_template: str = DEFAULT_WHOLE_TEMPLATE
    use_playall: bool = True
    include_bonus: bool = True
    mkvmerge: list[str] = dataclasses.field(default_factory=list)
    previews: bool = False
    ffmpeg: str = ""
    discdb: bool = False                 # look each disc up on TheDiscDB
    lookup: dict | None = None           # the show/movie matched online (lookup.Match.to_dict())
    lookup_key: str = ""                 # API key for that provider, if it needs one

    def plan_settings(self, mode: str) -> PlanSettings:
        return PlanSettings(mode, self.min_minutes, self.max_minutes, self.skip_duplicates,
                            self.skip_play_all, self.include_bonus)


class RipSession:
    """Waits for discs, scans them, rips the chosen titles and names (or joins) the files.

    Events sent through emit(kind, payload):
      log(text), status(text), progress((fraction or None, text)), titles(dict),
      row((title_index, status)), counters((next_disc, next_episode)),
      ask(("review" | "retry", payload)), fatal(text), stopped(None)
    """

    poll_interval = 2.0
    spinup_delay = 4.0

    def __init__(self, cfg: SessionConfig, emit, probe=disc_in_drive, ejector=eject_disc,
                 analyzer=analyze_disc_order, identifier=None):
        self.cfg = cfg
        self.emit = emit
        self.probe = probe
        self.ejector = ejector
        self.analyzer = analyzer
        self.identifier = identifier or identify_disc
        self.mkv = MakeMKV(cfg.command)
        self.disc = cfg.disc
        self.episode = cfg.episode
        self.scan_seconds = 30.0
        self.names = OnlineNames()       # from the show matched online, for the whole session
        self.disc_names = OnlineNames()  # for the disc in the drive (TheDiscDB can fill the title)
        self._stop = threading.Event()
        self._replies: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # --- control from the GUI thread ---

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.mkv.kill()
        self._replies.put(None)

    def reply(self, value) -> None:
        self._replies.put(value)

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    # --- helpers ---

    def log(self, text: str) -> None:
        self.emit("log", text)

    def status(self, text: str) -> None:
        self.emit("status", text)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise Stopped

    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise Stopped

    def _ask(self, kind: str, payload=None):
        while not self._replies.empty():
            self._replies.get_nowait()
        self.emit("ask", (kind, payload))
        value = self._replies.get()
        self._check_stop()
        return value

    @property
    def _tmp(self) -> Path:
        return self.cfg.out_dir / TMP_DIR_NAME

    def _name(self, titles: list[Title], mode: str, disc_no: int) -> int:
        c = self.cfg
        return assign_names(titles, mode, c.template, c.whole_template, c.show, disc_no, self.episode,
                            self.disc_names)

    def _load_online_names(self) -> None:
        """Fetch the matched show's official name and episode list once per session."""
        if not self.cfg.lookup:
            return
        match = lookup.Match.from_dict(self.cfg.lookup)
        self.names = OnlineNames(match.title, match.year)
        try:
            self.names.episodes = lookup.episodes(match, self.cfg.lookup_key)
        except lookup.OnlineError as e:
            self.log(f"Online names: couldn't get the episode list from {match.provider} ({e}); "
                     "the show's name and year will still be used.")
            return
        count = f", {len(self.names.episodes)} episode names" if self.names.episodes else ""
        self.log(f"Online names: {match.label} from {match.provider}{count}.")

    def _identify_disc(self, titles: list[Title]) -> None:
        """Label this disc's titles from TheDiscDB, if it knows the disc."""
        self.disc_names = OnlineNames(self.names.title, self.names.year, self.names.episodes)
        if not self.cfg.discdb:
            return
        self.emit("progress", (None, "Looking the disc up on TheDiscDB..."))
        match, line = self.identifier(self.cfg.drive.device)
        self.log(f"  {line}")
        if not match:
            return
        if not self.disc_names.title:
            self.disc_names.title, self.disc_names.year = match.title, match.year
        for t in titles:
            # DVD titles are keyed by their number on the disc, Blu-ray titles by playlist file.
            info = match.titles.get(f"{t.dvd_title:02}") if t.dvd_title else None
            info = info or match.titles.get(t.source)
            if info:
                t.online_type, t.online_season, t.online_episode = info.item_type, info.season, info.episode
                t.online_name = info.description

    # --- main loop ---

    def _run(self) -> None:
        c = self.cfg
        try:
            self.log(f"Session started for '{c.show}' at Disc {self.disc}, Episode {self.episode}. "
                     f"Saving to {c.out_dir}")
            self._load_online_names()
            need_empty = False
            while True:
                self._wait_for_disc(need_empty)
                self._process_disc()
                need_empty = True
        except Stopped:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self.log("Session stopped.")
        except FatalError as e:
            self.log(str(e))
            self.emit("fatal", str(e))
        except Exception as e:
            self.log(traceback.format_exc())
            self.emit("fatal", f"Unexpected error: {e}")
        finally:
            self.emit("progress", (0.0, ""))
            self.emit("stopped", None)

    def _wait_for_disc(self, need_empty: bool) -> None:
        device = self.cfg.drive.device
        shown = ""
        while True:
            present = self.probe(device)
            if need_empty:
                if present is None:
                    need_empty = False
                    continue
                message = f"Remove the disc from {device} and insert Disc {self.disc}."
            else:
                if present is not None:
                    self.status(f"Disc detected ({present[1] or 'no label'}), letting it spin up...")
                    self._sleep(self.spinup_delay)
                    return
                message = f"Waiting for Disc {self.disc} in drive {device}..."
            if message != shown:
                self.status(message)
                shown = message
            self._sleep(self.poll_interval)

    def _process_disc(self) -> None:
        c = self.cfg
        disc_no = self.disc

        self.status(f"Scanning Disc {disc_no}...")
        self.emit("progress", (None, "Scanning disc (Blu-rays can take a minute or two)..."))
        started = time.monotonic()
        info = self.mkv.info(c.drive.index)
        self.scan_seconds = max(5.0, time.monotonic() - started)
        self._check_stop()
        if err := license_error(info.messages):
            raise FatalError(f"MakeMKV refused to run:\n\n{err}\n\nOpen MakeMKV and update it or enter "
                             "a registration/beta key, then start the session again.")
        titles = info.titles
        self.log(f"Disc {disc_no} ('{info.name or 'no name'}'): MakeMKV found {len(titles)} title(s).")
        if not titles:
            for _, text in info.messages[-5:]:
                self.log(f"  MakeMKV: {text}")
            self.log("  Nothing to rip on this disc; it does not count toward the numbering.")
            self._finish_disc()
            return

        self._identify_disc(titles)
        self._check_stop()
        order, note = None, "disc order (reading the Play All order is turned off)"
        if c.use_playall:
            self.emit("progress", (None, "Reading the DVD menus to find the Play All order..."))
            # Only titles MakeMKV lists can be ripped; it quietly drops some (e.g. suspected decoys).
            listed = {t.dvd_title for t in titles if t.dvd_title}
            order, note = self.analyzer(c.drive.device, listed or None)
            self._check_stop()
        self.emit("progress", (0.0, ""))
        mode = c.mode
        self.log(f"  Order: {order_note(order, note, mode)}")
        for n, seconds in (order.repeats.items() if order and mode == WHOLE else []):
            self.log(f"  Title {n} is needed for video found nowhere else, but it also repeats "
                     f"{fmt_duration(int(seconds))} already included.")
        titles = build_plan(titles, order, c.plan_settings(mode))
        self._name(titles, mode, disc_no)
        skipped = [t for t in titles if t.skip_reason]
        for t in skipped[:12]:
            self.log(f"  Skipping title {t.index} ({fmt_duration(t.duration)}): {t.skip_reason}")
        if len(skipped) > 12:
            self.log(f"  ...and {len(skipped) - 12} more skipped (see the table).")

        def show(awaiting: bool) -> None:
            self.emit("titles", {"disc": disc_no, "first_episode": self.episode, "mode": mode,
                                 "order": order, "note": note, "review": awaiting, "names": self.disc_names,
                                 "titles": [dataclasses.replace(t) for t in titles]})

        if c.review:
            show(True)
            self.status(f"Disc {disc_no}: check the titles and order, then click Rip selected.")
            previews = self._start_previews(titles) if c.previews else None
            try:
                choice = self._ask("review")
            finally:
                if previews:  # stop reading the disc before MakeMKV needs it
                    previews[0].set()
                    previews[1].join(timeout=15)
            if choice is None:
                self.log(f"Disc {disc_no} skipped; it does not count toward the numbering.")
                self._finish_disc()
                return
            mode = choice["mode"]
            by_index = {t.index: t for t in titles}
            titles = [by_index[i] for i in choice["order"]]
            for t in titles:
                t.selected = t.index in choice["selected"]
                if t.selected:
                    t.skip_reason = ""
            self._name(titles, mode, disc_no)

        show(False)
        chosen = [t for t in titles if t.selected]
        if not chosen:
            self.log(f"No titles selected on Disc {disc_no}; it does not count toward the numbering.")
            self._finish_disc()
            return

        c.out_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self._tmp, ignore_errors=True)
        pending = chosen
        self.emit("ripping", True)
        try:
            while True:
                failed = self._rip(pending, mode, disc_no, titles)
                if not failed:
                    break
                names = "\n".join(t.part_name if mode == WHOLE else t.filename for t in failed)
                self.emit("ripping", False)
                if not self._ask("retry", names):
                    break
                self.emit("ripping", True)
                pending = failed
            if mode == WHOLE:
                self._join(chosen, failed)
        finally:
            self.emit("ripping", False)
        shutil.rmtree(self._tmp, ignore_errors=True)

        self.disc = disc_no + 1
        episodes = [t for t in chosen if t.kind == "episode"]
        if episodes:
            self.episode = episodes[-1].episode + 1
        self.emit("counters", (self.disc, self.episode))
        if mode == WHOLE:
            self.log(f"Disc {disc_no} done. Next up: Disc {self.disc}.")
        else:
            saved = len(chosen) - len(failed)
            self.log(f"Disc {disc_no} done: {saved} of {len(chosen)} file(s) saved. "
                     f"Next up: Disc {self.disc}, Episode {self.episode}.")
        self._finish_disc()

    def _start_previews(self, titles: list[Title]) -> tuple[threading.Event, threading.Thread]:
        """Grab preview frames in the background while the titles are being reviewed."""
        stop = threading.Event()

        def work() -> None:
            shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
            try:
                grabber = thumbs.PreviewGrabber(self.cfg.drive.device, PREVIEW_DIR, ffmpeg=self.cfg.ffmpeg)
            except (OSError, ValueError, IndexError):
                self.log("  Preview frames are only available for DVDs.")
                return
            except ImportError as e:
                self.log(f"  Preview frames aren't available: {e}.")
                return
            try:
                for t in [t for t in titles if t.selected] + [t for t in titles if not t.selected]:
                    if stop.is_set():
                        return
                    if not t.dvd_title:
                        continue
                    try:
                        frames = grabber.frames(t.dvd_title, stop)
                    except thumbs.CopyProtected as e:
                        self.log(f"  This DVD is copy-protected and preview frames can't be read before ripping ({e}).")
                        return
                    except (OSError, ValueError, IndexError) as e:
                        self.log(f"  No preview for title {t.dvd_title}: {e}")
                        continue
                    if frames and not stop.is_set():
                        self.emit("thumb", (t.index, [str(p) for p in frames]))
            finally:
                grabber.close()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return stop, thread

    def _rip(self, pending: list[Title], mode: str, disc_no: int, all_titles: list[Title]) -> list[Title]:
        rip_dir, parts_dir = self._tmp / "rip", self._tmp / "parts"
        one_run = rip_in_one_run(pending, all_titles, self.scan_seconds)
        if one_run:
            self.log(f"  Ripping all {len(all_titles)} titles in one MakeMKV run so the disc is only scanned "
                     f"once; the {len(all_titles) - len(pending)} not needed are deleted afterwards.")
        batches = [pending] if one_run else [[t] for t in pending]
        failed = []
        for n, batch in enumerate(batches):
            self._check_stop()
            shutil.rmtree(rip_dir, ignore_errors=True)
            rip_dir.mkdir(parents=True, exist_ok=True)
            if one_run:
                target = f"{len(pending)} titles"
                label = f"Disc {disc_no}: ripping {target} in one pass"
            else:
                t = batch[0]
                target = f"part {t.part} of {t.filename}" if mode == WHOLE else t.filename
                label = f"Disc {disc_no}: ripping {n + 1} of {len(batches)} -> {target}"
            self.status(label)
            for t in batch:
                self.emit("row", (t.index, "Ripping..."))
            last = -1
            messages: list[str] = []
            copy_started: float | None = None

            def on_line(line: str) -> None:
                nonlocal last, copy_started
                kind, f = parse_robot_line(line)
                if kind == "PRGV" and len(f) >= 3 and f[1].isdigit() and f[2].isdigit():
                    frac = int(f[1]) / max(int(f[2]), 1)
                    if int(frac * 1000) != last:
                        last = int(frac * 1000)
                        self.emit("progress", ((n + frac) / len(batches), f"{target}  {frac:.0%}"))
                elif kind == "MSG" and len(f) >= 4:
                    messages.append(f[3])
                    if f[3].startswith("Saving") and copy_started is None:
                        copy_started = time.monotonic()
                    if not (f[0].isdigit() and int(f[0]) in QUIET_MSG_CODES):
                        self.log(f"  MakeMKV: {f[3]}")

            code = self.mkv.rip(self.cfg.drive.index, "all" if one_run else batch[0].index, rip_dir, on_line)
            self._check_stop()
            self._log_speed(rip_dir, copy_started, code)
            for t in batch:
                name = f"part {t.part} of {t.filename}" if mode == WHOLE else t.filename
                src = self._ripped_file(t, rip_dir, one_run, code, messages)
                if src is None:
                    self.log(f"  FAILED: title {t.index} -> {name} (MakeMKV exit code {code})")
                    self.emit("row", (t.index, "FAILED"))
                    failed.append(t)
                    continue
                if mode == WHOLE:
                    parts_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(src, parts_dir / t.part_name)
                else:
                    dest = unique_path(self.cfg.out_dir / t.filename)
                    shutil.move(src, dest)
                    if dest.name != t.filename:
                        self.log(f"  {t.filename} already existed, so this one was saved as {dest.name}")
                    self.log(f"  Saved {dest}")
                self.emit("row", (t.index, "Ripped" if mode == WHOLE else "Saved"))
        shutil.rmtree(rip_dir, ignore_errors=True)
        self.emit("progress", (1.0, "Done"))
        return failed

    def _log_speed(self, rip_dir: Path, copy_started: float | None, code: int) -> None:
        """Note how fast this rip copied (for information; it isn't used for decisions)."""
        if code != 0 or copy_started is None:
            return
        seconds = time.monotonic() - copy_started
        copied = sum(p.stat().st_size for p in rip_dir.glob("*.mkv"))
        if seconds >= 20 and copied >= 50_000_000:
            self.log(f"  Copied at {copied / seconds / 1e6:.1f} MB/s.")

    @staticmethod
    def _ripped_file(t: Title, rip_dir: Path, one_run: bool, code: int, messages: list[str]) -> Path | None:
        """The file MakeMKV made for a title, or None if it failed."""
        if one_run:
            src = rip_dir / t.output_file if t.output_file else None
        else:
            files = sorted(rip_dir.glob("*.mkv"), key=lambda p: p.stat().st_size, reverse=True)
            src = files[0] if files else None
        if src is None or not src.exists() or src.stat().st_size == 0:
            return None
        if code != 0:
            # In a one-run rip, a non-zero exit means at least one title failed: drop the
            # ones MakeMKV names, and any that came out much smaller than the disc says.
            if not one_run or any(t.output_file in m and "fail" in m.lower() for m in messages):
                return None
            if t.size_bytes and src.stat().st_size < 0.85 * t.size_bytes:
                return None
        return src

    def _join(self, chosen: list[Title], failed: list[Title]) -> None:
        parts_dir = self._tmp / "parts"
        parts = [parts_dir / t.part_name for t in chosen if t not in failed]
        dest = unique_path(self.cfg.out_dir / chosen[0].filename)
        if not parts:
            return
        if failed:
            self._keep_parts(parts, dest, f"{len(failed)} part(s) failed to rip")
        elif len(parts) == 1:
            shutil.move(parts[0], dest)
            self.log(f"  Saved {dest}")
        elif not self.cfg.mkvmerge:
            self._keep_parts(parts, dest, "MKVToolNix isn't installed, so the parts couldn't be joined")
        else:
            self._pad_parts(parts)
            self.status(f"Joining {len(parts)} parts into {dest.name}...")
            self.emit("progress", (None, "Joining the parts with MKVToolNix..."))
            joined = self._tmp / "joined.mkv"
            ok, output = join_mkvs(self.cfg.mkvmerge, parts, joined)
            self.emit("progress", (1.0, "Done"))
            if ok:
                shutil.move(joined, dest)
                self.log(f"  Saved {dest} ({len(parts)} parts, a chapter at the start of each)")
            else:
                self.log(f"  mkvmerge couldn't join the parts:\n{output}")
                self._keep_parts(parts, dest, "the parts couldn't be joined (see the mkvmerge message above)")

    def _pad_parts(self, parts: list[Path]) -> None:
        if not self.cfg.ffmpeg:
            self.log("  ffmpeg isn't installed, so parts whose audio stops early (like title cards) can't be "
                     "padded; some players may skip through them in the joined file.")
            return
        self.status("Checking each part's audio before joining...")
        padded = []
        for n, part in enumerate(parts):
            self._check_stop()
            self.emit("progress", (n / len(parts), f"Checking audio: {part.stem}"))
            note = pad_short_audio(self.cfg.ffmpeg, part)
            if note.startswith("padded"):
                padded.append(part.stem)
            elif note:
                self.log(f"  {part.stem}: {note}")
        if padded:
            self.log(f"  Padded the audio of {len(padded)} part(s) that went silent early: {', '.join(padded)}")

    def _keep_parts(self, parts: list[Path], dest: Path, why: str) -> None:
        folder = unique_path(dest.with_name(f"{dest.stem} (parts)"))
        folder.mkdir(parents=True)
        for p in parts:
            shutil.move(p, folder / p.name)
        self.log(f"  {why[0].upper()}{why[1:]}. The {len(parts)} ripped part(s) are in {folder}, "
                 "numbered in Play All order.")

    def _finish_disc(self) -> None:
        if self.cfg.eject:
            self.status("Ejecting disc...")
            if not self.ejector(self.cfg.drive.device):
                self.log("Couldn't eject the disc; please take it out by hand.")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

THEMES = {
    "dark": dict(bg="#1f2023", panel="#2b2d31", field="#26282c", fg="#e4e6eb", muted="#9aa0a6",
                 border="#3d4046", accent="#4a8cff", select="#26476e", hover="#383b41",
                 bonus="#3b331f", full="#1d3a26", saved="#6fd47a", failed="#ff7b72", skipped="#767a80",
                 good="#39ff14"),
    "light": dict(bg="#f3f3f3", panel="#e4e4e4", field="#ffffff", fg="#1b1b1b", muted="#5f6368",
                  border="#c4c4c4", accent="#2f6fde", select="#cce0ff", hover="#d8d8d8",
                  bonus="#fff1cc", full="#dcf2e1", saved="#1a7f37", failed="#c62828", skipped="#9a9a9a",
                  good="#0b8a00"),
}
PREVIEW_HEIGHT = 54


def apply_theme(root: tk.Tk, name: str) -> dict:
    """Style every ttk widget from one palette; returns the palette for the non-ttk bits."""
    c = THEMES[name]
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["panel"],
                    focuscolor=c["accent"], selectbackground=c["select"], selectforeground=c["fg"],
                    insertcolor=c["fg"], arrowcolor=c["fg"], font=(UI_FONT, 9))
    style.map(".", foreground=[("disabled", c["muted"])])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"], font=(UI_FONT, 9, "bold"))
    style.configure("Muted.TLabel", foreground=c["muted"])
    style.configure("Warn.TLabel", foreground=c["failed"], font=(UI_FONT, 9, "bold"))
    style.configure("Good.TLabel", foreground=c["good"], font=(UI_FONT, 9, "bold"))
    style.configure("Status.TLabel", font=(UI_FONT, 10, "bold"))
    style.configure("TButton", background=c["panel"], bordercolor=c["border"], lightcolor=c["panel"],
                    darkcolor=c["panel"], padding=(10, 3))
    style.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["select"]), ("active", c["hover"])],
              lightcolor=[("active", c["hover"])], darkcolor=[("active", c["hover"])])
    for name, base, hover in (("Start.TButton", "#2e7d32", "#388e3c"), ("Rip.TButton", "#c62828", "#d32f2f")):
        style.configure(name, background=base, foreground="#ffffff", bordercolor=base,
                        lightcolor=base, darkcolor=base, font=(UI_FONT, 9, "bold"))
        style.map(name, background=[("disabled", c["panel"]), ("pressed", base), ("active", hover)],
                  foreground=[("disabled", c["muted"])], bordercolor=[("disabled", c["border"])],
                  lightcolor=[("disabled", c["panel"]), ("active", hover)],
                  darkcolor=[("disabled", c["panel"]), ("active", hover)])
    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=c["field"], foreground=c["fg"], background=c["panel"],
                        insertcolor=c["fg"], bordercolor=c["border"], arrowcolor=c["fg"],
                        lightcolor=c["field"], darkcolor=c["field"])
        style.map(widget, fieldbackground=[("disabled", c["bg"]), ("readonly", c["field"])],
                  foreground=[("disabled", c["muted"])], bordercolor=[("focus", c["accent"])],
                  background=[("active", c["hover"])])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=c["bg"], foreground=c["fg"], indicatorbackground=c["field"],
                        indicatorforeground="#ffffff", upperbordercolor=c["border"], lowerbordercolor=c["border"])
        style.map(widget, background=[("active", c["bg"])],
                  indicatorbackground=[("disabled", c["bg"]), ("selected", c["accent"]), ("pressed", c["select"])])
    style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"],
                    bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
    style.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["fg"])])
    style.configure("Treeview.Heading", background=c["panel"], foreground=c["fg"], bordercolor=c["border"],
                    lightcolor=c["panel"], darkcolor=c["panel"], relief="flat")
    style.map("Treeview.Heading", background=[("active", c["hover"])])
    style.configure("TProgressbar", background=c["accent"], troughcolor=c["panel"], bordercolor=c["border"],
                    lightcolor=c["accent"], darkcolor=c["accent"])
    style.configure("TScrollbar", background=c["panel"], troughcolor=c["bg"], bordercolor=c["bg"],
                    lightcolor=c["panel"], darkcolor=c["panel"], arrowcolor=c["fg"])
    style.map("TScrollbar", background=[("active", c["hover"])])
    root.configure(background=c["bg"])
    for option, key in (("background", "field"), ("foreground", "fg"),
                        ("selectBackground", "select"), ("selectForeground", "fg")):
        root.option_add(f"*TCombobox*Listbox.{option}", c[key])
    _dark_title_bar(root, name == "dark")
    return c


def _dark_title_bar(root: tk.Tk, dark: bool) -> None:
    """Ask Windows 10/11 to draw the title bar dark (attribute 20, or 19 on older builds)."""
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        if root.winfo_ismapped():  # Windows only repaints the frame when the window is re-shown
            root.withdraw()
            root.deiconify()
    except (AttributeError, OSError):
        pass


TIPS = {
    "show": "Used for the output folder name and the {show} placeholder.",
    "out": "Folder for finished files. Work files go in a .gongoautoripper-tmp folder inside it and are removed "
           "after each disc.",
    "subfolder": "Save into <Save to>\\<Show name> instead of straight into <Save to>.",
    "episodes": "Each ticked title is saved as its own MKV and named with the episode counter.",
    "whole": "The ticked titles are ripped and joined into one MKV with mkvmerge, with a chapter at the "
             "start of each part. On DVDs the titles are chosen so every video segment on the disc is "
             "included once, in playback order.",
    "template": "File name for each episode. Placeholders: {show} {disc} {episode} {title} {year} {season} "
                "{season_episode} {episode_title}. Number formats work, e.g. {episode:02} gives 01. "
                "Online placeholders that aren't known are left out.",
    "whole_template": "File name for one-file-per-disc rips. Same placeholders as the episode name.",
    "numbers": "Counters used in file names. They go up after each finished disc and are saved per show.",
    "discdb": "Identifies the disc on thediscdb.com from a hash of its file sizes. If the disc is known, "
              "each title comes with a label (episode, extra, trailer) that sets the selection and names "
              "in episodes mode. No API key needed.",
    "sources": "Databases searched by Find show for the show's official name and episode names. TMDb and "
               "OMDb need a free API key; TVmaze doesn't.",
    "keys": "API keys are saved in GongoAutoRipper's settings.json in plain text.",
    "find": "Search the ticked databases and pick the match used for {title}, {year} and the episode "
            "placeholders. The pick is saved per show.",
    "drive": "Drive as listed by MakeMKV. Refresh lists drives again.",
    "eject": "Open the tray when a disc is finished or skipped.",
    "playall": "Reads the DVD's IFO files and steps through the menu program to find what Play All plays "
               "and in which order. Sets the episode order, and the part order for one-file rips. DVDs only.",
    "bonus": "Titles that aren't part of the main playback, such as extras. Episodes mode saves them as "
             "separate Bonus files; one-file rips add them after the main playback.",
    "dups": "Untick titles with the same segment map, length and size as an earlier title.",
    "playall_title": "If no menu order was found, untick a title whose length equals the other titles "
                     "added together (a play-all compilation). Episodes mode only.",
    "lengths": "Untick titles shorter than the first value or longer than the second (0 = no limit). "
               "Applies in separate-episodes mode; one-file rips choose titles by playback order instead.",
    "review": "Stop after each disc scan so you can change the ticks, order and mode before ripping. "
              "When off, ripping starts straight away.",
    "previews": "While reviewing, grab 3 frames from each DVD title (start, middle, end). Protected DVDs need "
                "libdvdcss; the app offers to install it. Click a strip to enlarge it.",
    "makemkvcon": "MakeMKV's command-line program. It scans the disc and rips the titles.",
    "mkvmerge": "From MKVToolNix. Joins the parts of one-file rips.",
    "ffmpeg": "Decodes preview frames and pads short audio tracks with silence before joining.",
    "disc_mode": "Change the mode for this disc only. The next disc goes back to the default mode.",
    "dark": "Switch between the dark and light theme.",
}


class Tooltip:
    """A small note shown when the mouse rests on a widget."""

    def __init__(self, widget, text: str, app: App):
        self.widget, self.text, self.app = widget, text, app
        self.window = None
        self.pending = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self.pending = self.widget.after(500, self._show)

    def _show(self) -> None:
        self.pending = None
        c = self.app.colors
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_attributes("-topmost", True)
        self.window.wm_geometry(f"+{x}+{y}")
        tk.Label(self.window, text=self.text, justify="left", wraplength=380, background=c["panel"],
                 foreground=c["fg"], relief="solid", borderwidth=1, font=(UI_FONT, 9),
                 padx=8, pady=5).pack()

    def _hide(self, _event=None) -> None:
        if self.pending:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window:
            self.window.destroy()
            self.window = None


class IconAnimator:
    """Plays the icon GIF as the window's icon while a rip runs: the title bar and taskbar
    on Windows, the Dock on macOS, the window icon on Linux (X11).

    Icon files can't animate on any of them, so the GIF's frames are swapped in on a timer
    and the still icon goes back when the rip ends.
    """

    SIZES = (16, 32, 64)

    def __init__(self, root: tk.Tk, gif: Path, restore):
        self.root, self.restore = root, restore
        self.frames: list[list] = []   # one image per size, for each frame
        self.delay = 80
        self.job = None
        self.index = 0
        if Image is None or not gif.exists():
            return
        try:
            with Image.open(gif) as im:
                self.delay = max(40, im.info.get("duration") or self.delay)
                frames = []
                for n in range(getattr(im, "n_frames", 1)):
                    im.seek(n)
                    frames.append(im.convert("RGBA"))
        except OSError:
            return
        # Trim the empty border shared by every frame, then square it up.
        boxes = [f.getbbox() for f in frames if f.getbbox()]
        if not boxes:
            return
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        for frame in frames:
            art = frame.crop(box)
            side = max(art.size)
            square = Image.new("RGBA", (side, side))
            square.paste(art, ((side - art.width) // 2, (side - art.height) // 2))
            self.frames.append([ImageTk.PhotoImage(self._fit(square, size)) for size in self.SIZES])

    @staticmethod
    def _fit(art, size: int):
        """Pixel art scaled by a whole number with no smoothing, centred on a size x size canvas."""
        scale = size // art.width
        if scale < 1:
            return art.resize((size, size), Image.Resampling.LANCZOS)
        big = art.resize((art.width * scale, art.height * scale), Image.Resampling.NEAREST)
        out = Image.new("RGBA", (size, size))
        out.paste(big, ((size - big.width) // 2, (size - big.height) // 2))
        return out

    def start(self) -> None:
        if self.frames and self.job is None:
            self._tick()

    def stop(self) -> None:
        if self.job is None:
            return
        self.root.after_cancel(self.job)
        self.job = None
        self.restore()

    def _tick(self) -> None:
        try:
            self.root.iconphoto(False, *self.frames[self.index])
        except tk.TclError:
            self.job = None
            return
        self.index = (self.index + 1) % len(self.frames)
        self.job = self.root.after(self.delay, self._tick)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.session: RipSession | None = None
        self.drives: list[Drive] = []
        self.titles: list[Title] = []
        self.review: dict | None = None
        self.inputs: list[ttk.Widget] = []

        s = self.settings
        self.v_show = tk.StringVar(value=s.get("show", ""))
        self.v_out = tk.StringVar(value=s.get("out_root", str(Path.home() / "Videos")))
        self.v_subfolder = tk.BooleanVar(value=s.get("subfolder", True))
        self.v_mode = tk.StringVar(value=s.get("mode", EPISODES))
        self.v_template = tk.StringVar(value=s.get("template", DEFAULT_TEMPLATE))
        self.v_whole_template = tk.StringVar(value=s.get("whole_template", DEFAULT_WHOLE_TEMPLATE))
        self.v_disc = tk.StringVar(value=str(s.get("disc", 1)))
        self.v_episode = tk.StringVar(value=str(s.get("episode", 1)))
        self.v_drive = tk.StringVar()
        self.v_min = tk.StringVar(value=str(s.get("min_minutes", 10)))
        self.v_max = tk.StringVar(value=str(s.get("max_minutes", 0)))
        self.v_dups = tk.BooleanVar(value=s.get("skip_duplicates", True))
        self.v_playall_title = tk.BooleanVar(value=s.get("skip_play_all", True))
        self.v_use_playall = tk.BooleanVar(value=s.get("use_playall", True))
        self.v_bonus = tk.BooleanVar(value=s.get("include_bonus", True))
        self.v_review = tk.BooleanVar(value=s.get("review", False))
        self.v_eject = tk.BooleanVar(value=s.get("eject", True))
        self.v_exe = tk.StringVar(value=s.get("makemkvcon") or find_makemkvcon())
        self.v_mkvmerge = tk.StringVar(value=s.get("mkvmerge") or find_mkvmerge())
        self.v_ffmpeg = tk.StringVar(value=s.get("ffmpeg") or find_ffmpeg())
        self.v_discdb = tk.BooleanVar(value=s.get("discdb", True))
        sources = s.get("lookup_sources", ["TVmaze"])
        self.v_sources = {p: tk.BooleanVar(value=p in sources) for p in lookup.PROVIDERS}
        self.v_keys = {p: tk.StringVar(value=s.get("api_keys", {}).get(p, ""))
                       for p in lookup.PROVIDERS if p in lookup.NEEDS_KEY}
        self.v_match = tk.StringVar()
        self.v_status = tk.StringVar(value="Idle. Fill in the series details and click Start session.")
        self.v_progress = tk.StringVar()
        self.v_preview = tk.StringVar()
        self.v_order = tk.StringVar()
        self.v_summary = tk.StringVar()   # what the rip makes (green)
        self.v_dropped = tk.StringVar()   # what it leaves out (red)
        self.v_disc_mode = tk.StringVar(value=EPISODES)
        self.v_dark = tk.BooleanVar(value=s.get("theme", "dark") == "dark")
        self.v_previews = tk.BooleanVar(value=s.get("previews", True) and not MISSING_FOR_PREVIEWS)
        self.previews: dict[int, tuple[object, list[str]]] = {}  # title index -> (strip image, frame files)
        self.colors = THEMES["dark"]

        root.title("GongoAutoRipper - MakeMKV series ripper")
        self._set_still_icon(default=True)
        self.icon_animation = IconAnimator(root, ICON_DIR / "fireorb.gif", self._set_still_icon)
        root.geometry("1060x960")
        root.minsize(860, 760)
        root.report_callback_exception = self._on_tk_error
        self._build()
        self._apply_theme()

        self.v_show.trace_add("write", self._on_show_changed)
        for var in (self.v_show, self.v_template, self.v_whole_template, self.v_disc, self.v_episode, self.v_mode):
            var.trace_add("write", lambda *_: self._update_preview())
        for var in (self.v_discdb, *self.v_sources.values(), *self.v_keys.values()):
            var.trace_add("write", lambda *_: self._update_match_label())
        self._update_match_label()
        if MISSING_FOR_PREVIEWS:
            verb = "isn't" if len(MISSING_FOR_PREVIEWS) == 1 else "aren't"
            self.log(f"Preview frames are off: {' and '.join(MISSING_FOR_PREVIEWS)} {verb} installed for this "
                     f"Python. Install with: py -m pip install {' '.join(MISSING_FOR_PREVIEWS)}")
        # Offer libdvdcss whenever previews are switched on, and once at startup when they're on already.
        self.v_previews.trace_add("write", lambda *_: self.v_previews.get() and root.after_idle(self._offer_libdvdcss))
        if not self.settings.get("libdvdcss_asked"):
            root.after(800, self._offer_libdvdcss)

        self.refresh_drives()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- layout ---

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=3)
        root.rowconfigure(5, weight=2)

        def remember(widget):
            self.inputs.append(widget)
            return widget

        def tip(widget, key: str):
            Tooltip(widget, TIPS[key], self)
            return widget

        series = ttk.LabelFrame(root, text="Series", padding=8)
        series.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        series.columnconfigure(1, weight=1)
        tip(ttk.Label(series, text="Show name"), "show").grid(row=0, column=0, sticky="w", padx=(0, 8))
        tip(remember(ttk.Entry(series, textvariable=self.v_show)), "show").grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=2)
        tip(ttk.Label(series, text="Save to"), "out").grid(row=1, column=0, sticky="w")
        tip(remember(ttk.Entry(series, textvariable=self.v_out)), "out").grid(row=1, column=1, sticky="ew", pady=2)
        remember(ttk.Button(series, text="Browse...", command=self.browse_out)).grid(row=1, column=2, padx=(6, 0))
        tip(remember(ttk.Checkbutton(series, text="Put the files in a folder named after the show",
                                     variable=self.v_subfolder)), "subfolder").grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Label(series, text="Default mode").grid(row=3, column=0, sticky="w")
        modes = ttk.Frame(series)
        modes.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        tip(remember(ttk.Radiobutton(modes, text="Separate episodes", value=EPISODES,
                                     variable=self.v_mode)), "episodes").pack(side="left")
        tip(remember(ttk.Radiobutton(modes, text="One file per disc", value=WHOLE,
                                     variable=self.v_mode)), "whole").pack(side="left", padx=(16, 0))
        ttk.Label(modes, text="(can be changed for a single disc while reviewing)",
                  style="Muted.TLabel").pack(side="left", padx=12)
        tip(ttk.Label(series, text="Episode file name"), "template").grid(row=4, column=0, sticky="w")
        tip(remember(ttk.Entry(series, textvariable=self.v_template)), "template").grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=2)
        tip(ttk.Label(series, text="Whole-disc file name"), "whole_template").grid(row=5, column=0, sticky="w", padx=(0, 8))
        tip(remember(ttk.Entry(series, textvariable=self.v_whole_template)), "whole_template").grid(
            row=5, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Label(series, textvariable=self.v_preview, style="Muted.TLabel").grid(row=6, column=1, columnspan=2, sticky="w")
        tip(ttk.Label(series, text="Numbering"), "numbers").grid(row=7, column=0, sticky="w")
        nums = ttk.Frame(series)
        nums.grid(row=7, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(nums, text="Next disc #").pack(side="left")
        tip(remember(ttk.Spinbox(nums, from_=1, to=999, width=5, textvariable=self.v_disc)), "numbers").pack(
            side="left", padx=(4, 18))
        ttk.Label(nums, text="Next episode #").pack(side="left")
        tip(remember(ttk.Spinbox(nums, from_=1, to=9999, width=6, textvariable=self.v_episode)), "numbers").pack(
            side="left", padx=4)
        ttk.Label(series, text="Online names").grid(row=8, column=0, sticky="w")
        online = ttk.Frame(series)
        online.grid(row=8, column=1, columnspan=2, sticky="w", pady=(6, 0))
        tip(remember(ttk.Checkbutton(online, text="Look discs up on TheDiscDB", variable=self.v_discdb)),
            "discdb").pack(side="left")
        tip(ttk.Label(online, text="    Search for show names on"), "sources").pack(side="left")
        for provider in lookup.PROVIDERS:
            tip(remember(ttk.Checkbutton(online, text=provider, variable=self.v_sources[provider])),
                "sources").pack(side="left", padx=(6, 0))
        tip(remember(ttk.Button(online, text="Find show...", command=self.find_show)), "find").pack(
            side="left", padx=(14, 0))
        keys = ttk.Frame(series)
        keys.grid(row=9, column=1, columnspan=2, sticky="w", pady=(4, 0))
        for provider in self.v_keys:
            tip(ttk.Label(keys, text=f"{provider} API key"), "keys").pack(side="left", padx=(0, 4))
            tip(remember(ttk.Entry(keys, textvariable=self.v_keys[provider], width=34, show="•")), "keys").pack(
                side="left", padx=(0, 14))
        ttk.Label(series, textvariable=self.v_match, style="Muted.TLabel").grid(row=10, column=1, columnspan=2, sticky="w")

        options = ttk.Frame(root, padding=(10, 0))
        options.grid(row=1, column=0, sticky="ew", pady=4)
        options.columnconfigure(1, weight=1)

        drive = ttk.LabelFrame(options, text="Drive", padding=8)
        drive.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.drive_box = tip(remember(ttk.Combobox(drive, textvariable=self.v_drive, state="readonly", width=34)), "drive")
        self.drive_box.grid(row=0, column=0, sticky="w", pady=2)
        tip(remember(ttk.Button(drive, text="Refresh", command=self.refresh_drives)), "drive").grid(
            row=0, column=1, padx=(6, 0))
        tip(remember(ttk.Checkbutton(drive, text="Eject disc when done", variable=self.v_eject)), "eject").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        choose = ttk.LabelFrame(options, text="Choosing titles", padding=8)
        choose.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        for row, (text, var, key) in enumerate((
                ("Follow the DVD menu's Play All order", self.v_use_playall, "playall"),
                ("Include extras outside the main playback", self.v_bonus, "bonus"),
                ("Skip duplicate titles", self.v_dups, "dups"),
                ("Skip \"play all\" compilations when there's no menu order", self.v_playall_title, "playall_title"))):
            tip(remember(ttk.Checkbutton(choose, text=text, variable=var)), key).grid(row=row, column=0, sticky="w")
        lengths = ttk.Frame(choose)
        lengths.grid(row=4, column=0, sticky="w", pady=(6, 0))
        tip(ttk.Label(lengths, text="Title length"), "lengths").pack(side="left")
        tip(remember(ttk.Spinbox(lengths, from_=0, to=300, width=5, textvariable=self.v_min)), "lengths").pack(
            side="left", padx=4)
        ttk.Label(lengths, text="to").pack(side="left")
        tip(remember(ttk.Spinbox(lengths, from_=0, to=600, width=5, textvariable=self.v_max)), "lengths").pack(
            side="left", padx=4)
        ttk.Label(lengths, text="minutes  (0 = no limit)").pack(side="left")

        review_box = ttk.LabelFrame(options, text="Review", padding=8)
        review_box.grid(row=0, column=2, sticky="nsew")
        tip(remember(ttk.Checkbutton(review_box, text="Review titles before ripping", variable=self.v_review)),
            "review").grid(row=0, column=0, sticky="w")
        tip(ttk.Label(review_box, text="Disables full automation: waits for you after each scan",
                      style="Muted.TLabel", wraplength=190), "review").grid(row=1, column=0, sticky="w",
                                                                           padx=(22, 0), pady=(0, 4))
        previews = tip(ttk.Checkbutton(review_box, text="Show preview frames (DVDs)", variable=self.v_previews),
                       "previews")
        previews.grid(row=2, column=0, sticky="w")
        if MISSING_FOR_PREVIEWS:
            previews.configure(text=f"Preview frames need {' and '.join(MISSING_FOR_PREVIEWS)}")
            previews.state(["disabled"])
        else:
            remember(previews)

        controls = ttk.Frame(root, padding=(10, 6))
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(controls, text="Start session", command=self.start, style="Start.TButton")
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=6)
        ttk.Label(controls, textvariable=self.v_status, style="Status.TLabel",
                  wraplength=700).grid(row=0, column=2, sticky="w", padx=8)
        tip(ttk.Checkbutton(controls, text="Dark mode", variable=self.v_dark,
                            command=self._apply_theme), "dark").grid(row=0, column=3, sticky="e")

        table = ttk.Frame(root, padding=(10, 0))
        table.grid(row=3, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        ttk.Label(table, textvariable=self.v_order, style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
        columns = ("rip", "order", "title", "length", "size", "name", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings", height=9, selectmode="browse")
        self.tree.heading("#0", text="Preview")
        self.tree.column("#0", width=0, minwidth=0, stretch=False)
        for col, text, width, stretch in (("rip", "Rip", 40, False), ("order", "Order", 150, False),
                                          ("title", "Title", 50, False), ("length", "Length", 70, False),
                                          ("size", "Size", 80, False), ("name", "Saved as", 360, True),
                                          ("status", "Status", 90, False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor="w" if col in ("name", "order") else "center")
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        review = ttk.Frame(table)
        review.grid(row=2, column=0, sticky="w", pady=4)
        self.review_widgets = []
        for text, command, style, gap in (
                ("Rip selected", self.review_rip, "Rip.TButton", 6),
                ("Check all", self.review_check_all, "TButton", 6),
                ("Skip this disc", self.review_skip, "TButton", 18),
                ("Move to top", lambda: self.review_move_to(0), "TButton", 6),
                ("Move up", lambda: self.review_move(-1), "TButton", 6),
                ("Move down", lambda: self.review_move(1), "TButton", 6),
                ("Move to bottom", lambda: self.review_move_to(None), "TButton", 6)):
            btn = ttk.Button(review, text=text, command=command, style=style, state="disabled")
            btn.pack(side="left", padx=(0, gap))
            self.review_widgets.append(btn)
        self.check_all_btn = self.review_widgets[1]
        tip(ttk.Label(review, text="   This disc only:"), "disc_mode").pack(side="left")
        for text, value in (("Separate episodes", EPISODES), ("One file", WHOLE)):
            rb = tip(ttk.Radiobutton(review, text=text, value=value, variable=self.v_disc_mode,
                                     command=self._review_mode_changed, state="disabled"), "disc_mode")
            rb.pack(side="left", padx=4)
            self.review_widgets.append(rb)
        ttk.Label(table, textvariable=self.v_summary, style="Good.TLabel").grid(row=3, column=0, sticky="w")
        self.dropped_label = ttk.Label(table, textvariable=self.v_dropped, style="Warn.TLabel")
        self.dropped_label.grid(row=4, column=0, sticky="w")
        self.review_hint = ttk.Label(table, text="", style="Muted.TLabel")
        self.review_hint.grid(row=5, column=0, sticky="w")

        prog = ttk.Frame(root, padding=(10, 2))
        prog.grid(row=4, column=0, sticky="ew")
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.v_progress, width=60).grid(row=0, column=1, sticky="w", padx=8)

        logf = ttk.LabelFrame(root, text="Log", padding=4)
        logf.grid(row=5, column=0, sticky="nsew", padx=10, pady=4)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=7, wrap="word", state="disabled", font=(MONO_FONT, 9),
                                relief="flat", borderwidth=0, highlightthickness=0)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        footer = ttk.LabelFrame(root, text="Programs", padding=8)
        footer.grid(row=6, column=0, sticky="ew", padx=10, pady=(4, 10))
        footer.columnconfigure(1, weight=1)
        for row, (label, var, browse, key) in enumerate((
                ("makemkvcon", self.v_exe, self.browse_exe, "makemkvcon"),
                ("mkvmerge", self.v_mkvmerge, self.browse_mkvmerge, "mkvmerge"),
                ("ffmpeg", self.v_ffmpeg, self.browse_ffmpeg, "ffmpeg"))):
            pad = (0, 0) if row == 0 else (4, 0)
            tip(ttk.Label(footer, text=label), key).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=pad)
            tip(remember(ttk.Entry(footer, textvariable=var)), key).grid(row=row, column=1, sticky="ew", pady=pad)
            remember(ttk.Button(footer, text="Browse...", command=browse)).grid(row=row, column=2, padx=(6, 0), pady=pad)

    # --- theme and previews ---

    def _apply_theme(self) -> None:
        c = self.colors = apply_theme(self.root, "dark" if self.v_dark.get() else "light")
        self.log_text.configure(background=c["field"], foreground=c["fg"], insertbackground=c["fg"],
                                selectbackground=c["select"], selectforeground=c["fg"])
        self.tree.tag_configure("skipped", foreground=c["skipped"])
        self.tree.tag_configure("bonus", background=c["bonus"])
        self.tree.tag_configure("full", background=c["full"])
        self.tree.tag_configure("failed", foreground=c["failed"])
        self.tree.tag_configure("saved", foreground=c["saved"])
        self.settings["theme"] = "dark" if self.v_dark.get() else "light"

    def _set_preview_column(self, show: bool) -> None:
        style = ttk.Style(self.root)
        # Taller preview rows get fewer of them so the table doesn't push the log out of the window.
        if show:
            self.tree.column("#0", width=3 * PREVIEW_HEIGHT * 16 // 9 + 16, minwidth=60, stretch=False)
            style.configure("Treeview", rowheight=PREVIEW_HEIGHT + 6)
            self.tree.configure(height=4)
        else:
            self.tree.column("#0", width=0, minwidth=0, stretch=False)
            style.configure("Treeview", rowheight=22)
            self.tree.configure(height=9)

    @staticmethod
    def _strip(paths: list[str]):
        """The frames side by side at row height, on a transparent background."""
        frames = []
        for p in paths:
            with Image.open(p) as im:
                frames.append(im.convert("RGBA").resize(
                    (round(im.width * PREVIEW_HEIGHT / im.height), PREVIEW_HEIGHT), Image.Resampling.LANCZOS))
        strip = Image.new("RGBA", (sum(f.width for f in frames) + 3 * (len(frames) - 1), PREVIEW_HEIGHT), (0, 0, 0, 0))
        x = 0
        for f in frames:
            strip.paste(f, (x, 0))
            x += f.width + 3
        return ImageTk.PhotoImage(strip)

    def _add_preview(self, index: int, paths: list[str]) -> None:
        try:
            image = self._strip(paths)
        except OSError:
            return
        self.previews[index] = (image, paths)
        if self.tree.exists(str(index)):
            self.tree.item(str(index), image=image)

    def _open_preview(self, index: int) -> None:
        if index not in self.previews:
            return
        title = next((t for t in self.titles if t.index == index), None)
        win = tk.Toplevel(self.root)
        win.title(f"Title {title.dvd_title or title.index} - {fmt_duration(title.duration)}" if title else "Preview")
        win.configure(background=self.colors["bg"])
        win.transient(self.root)
        win.images = []
        for col, (path, label) in enumerate(zip(self.previews[index][1], ("Start", "Middle", "End"))):
            try:
                image = ImageTk.PhotoImage(Image.open(path))
            except OSError:
                continue
            win.images.append(image)
            ttk.Label(win, image=image).grid(row=0, column=col, padx=6, pady=(10, 2))
            ttk.Label(win, text=label, style="Muted.TLabel").grid(row=1, column=col, pady=(0, 10))
        _dark_title_bar(win, self.v_dark.get())
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_set()

    # --- small helpers ---

    @staticmethod
    def _num(var: tk.StringVar, kind=int):
        try:
            return kind(var.get().strip())
        except ValueError:
            return None

    def _out_dir(self) -> Path:
        root = Path(self.v_out.get().strip())
        show = self.v_show.get().strip()
        return root / safe_filename(show) if self.v_subfolder.get() and show else root

    def _update_preview(self) -> None:
        disc = self._num(self.v_disc) or 1
        episode = self._num(self.v_episode) or 1
        show = self.v_show.get().strip() or "Show"
        template = self.v_whole_template.get() if self.v_mode.get() == WHOLE else self.v_template.get()
        if problem := template_problem(template):
            self.v_preview.set(f"That file name pattern {problem}.")
            return
        match = self._match()
        online = {"title": match["title"], "year": match["year"]} if match else {}
        note = ""
        if re.search(r"\{(season|season_episode|episode_title)\b", template):
            note = (f"   (season and episode names are filled in from {match['provider']} when ripping)" if match
                    else "   (season and episode names need TheDiscDB to know the disc, or a matched show)")
        self.v_preview.set(f"Next file: {format_name(template, show, disc, episode, **online)}{note}")

    def _on_show_changed(self, *_) -> None:
        if self.session and self.session.running:
            return
        saved = self.settings.get("shows", {}).get(self.v_show.get().strip())
        if saved:
            self.v_disc.set(str(saved["disc"]))
            self.v_episode.set(str(saved["episode"]))
            if "mode" in saved:
                self.v_mode.set(saved["mode"])
        self._update_match_label()

    # --- online names ---

    def _sources(self) -> list[str]:
        return [p for p in lookup.PROVIDERS if self.v_sources[p].get()]

    def _key(self, provider: str) -> str:
        return self.v_keys[provider].get().strip() if provider in self.v_keys else ""

    def _match(self) -> dict | None:
        """The online match picked for this show, if its source is still ticked."""
        match = self.settings.get("shows", {}).get(self.v_show.get().strip(), {}).get("lookup")
        return match if match and match.get("provider") in self._sources() else None

    def _update_match_label(self) -> None:
        sources = self._sources()
        if not sources:
            text = "Show-name search is off." + (" Discs are still looked up on TheDiscDB." if self.v_discdb.get() else "")
        elif match := self._match():
            text = (f"Using {match['provider']}: {lookup.Match.from_dict(match).label}. "
                    "Click Find show to compare or change.")
        else:
            missing = [p for p in sources if p in lookup.NEEDS_KEY and not self._key(p)]
            text = (f"No show picked yet: click Find show to search {', '.join(sources)}."
                    + (f" ({', '.join(missing)} also need{'s' if len(missing) == 1 else ''} an API key.)" if missing else ""))
        self.v_match.set(text)
        self._update_preview()

    def _naming_preview(self, match: lookup.Match, eps: list) -> str:
        """How files would be named with this match: the next few episodes, or the whole-disc name."""
        show = self.v_show.get().strip()
        disc, first = self._num(self.v_disc) or 1, self._num(self.v_episode) or 1
        names = OnlineNames(match.title, match.year, eps)
        lines = [match.label, f"from {match.provider}", ""]
        if eps:
            seasons = len({e.season for e in eps})
            lines.append(f"{seasons} season{'s' if seasons != 1 else ''}, {len(eps)} episodes")
        elif match.kind == "tv":
            lines.append("No episode list available.")
        lines += ["", f"Your next files (Disc {disc}, from episode {first}):"]
        if self.v_mode.get() == WHOLE or match.kind != "tv":
            lines.append("  " + format_name(self.v_whole_template.get(), show, disc, first, **names.fields()))
        else:
            for n in range(first, first + 4):
                lines.append("  " + format_name(self.v_template.get(), show, disc, n, **names.fields(n)))
            if first > len(eps):
                lines.append(f"  (this source lists {len(eps)} episodes, so episode {first} has no name here)")
        if eps:
            lines += ["", "Its episode list starts:"]
            lines += [f"  {i}. S{e.season:02}E{e.number:02}  {e.title}" for i, e in enumerate(eps[:6], start=1)]
        return "\n".join(lines)

    def find_show(self) -> None:
        show = self.v_show.get().strip()
        sources = self._sources()
        if not sources:
            messagebox.showinfo(APP_NAME, "Tick at least one source to search (TMDb, OMDb or TVmaze).")
            return
        if not show:
            messagebox.showinfo(APP_NAME, "Enter the show name first.")
            return
        c = self.colors
        win = tk.Toplevel(self.root)
        win.title(f"Find '{show}' online")
        win.geometry("1080x560")
        win.configure(background=c["bg"])
        win.transient(self.root)
        _dark_title_bar(win, self.v_dark.get())
        query = tk.StringVar(value=show)
        top = ttk.Frame(win, padding=10)
        top.pack(fill="x")
        ttk.Entry(top, textvariable=query, width=50).pack(side="left", fill="x", expand=True)
        body = ttk.Frame(win, padding=(10, 0))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        tree = ttk.Treeview(body, columns=("source", "title", "year", "type", "detail"), show="headings",
                            selectmode="browse")
        for col, text, width in (("source", "Source", 70), ("title", "Title", 260), ("year", "Year", 50),
                                 ("type", "Type", 70), ("detail", "Detail", 110)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        preview = tk.Text(body, width=48, wrap="word", state="disabled", relief="flat", borderwidth=0,
                          highlightthickness=0, font=(MONO_FONT, 9), background=c["field"], foreground=c["fg"],
                          padx=8, pady=6)
        preview.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        info = ttk.Label(win, text="", style="Muted.TLabel")
        info.pack(anchor="w", padx=10, pady=4)
        found: dict[str, lookup.Match] = {}
        episode_cache: dict[str, list] = {}

        def show_preview(text: str) -> None:
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("end", text)
            preview.configure(state="disabled")

        def run_search() -> None:
            win.configure(cursor="watch")
            win.update_idletasks()
            tree.delete(*tree.get_children())
            found.clear()
            notes = []
            for provider in sources:
                if provider in lookup.NEEDS_KEY and not self._key(provider):
                    notes.append(f"{provider}: needs an API key")
                    continue
                try:
                    matches = lookup.search(provider, self._key(provider), query.get())
                except lookup.OnlineError as e:
                    notes.append(f"{provider}: {e}")
                    continue
                notes.append(f"{provider}: {len(matches)} found")
                for m in matches:
                    iid = f"{m.provider}:{m.id}"
                    found[iid] = m
                    kind = {"tv": "TV series", "movie": "Movie"}.get(m.kind, m.kind)
                    tree.insert("", "end", iid=iid, values=(m.provider, m.title, m.year, kind, m.note))
            win.configure(cursor="")
            info.configure(text="   ·   ".join(notes))
            show_preview("Select a result to see how it would name your files." if found else
                         "Nothing found. Try a shorter or different name, or tick more sources.")

        def selected() -> lookup.Match | None:
            pick = tree.selection()
            return found.get(pick[0]) if pick else None

        def on_select(_event=None) -> None:
            match = selected()
            if not match:
                return
            iid = f"{match.provider}:{match.id}"
            if iid not in episode_cache:
                win.configure(cursor="watch")
                win.update_idletasks()
                try:
                    episode_cache[iid] = lookup.episodes(match, self._key(match.provider))
                except lookup.OnlineError as e:
                    episode_cache[iid] = []
                    info.configure(text=f"{match.provider}: couldn't get the episode list ({e})")
                finally:
                    win.configure(cursor="")
            show_preview(self._naming_preview(match, episode_cache[iid]))

        def use() -> None:
            match = selected()
            if not match:
                return
            entry = self.settings.setdefault("shows", {}).setdefault(show, {"disc": self._num(self.v_disc) or 1,
                                                                             "episode": self._num(self.v_episode) or 1})
            entry["lookup"] = match.to_dict()
            self.save()
            self._update_match_label()
            win.destroy()

        def clear() -> None:
            self.settings.get("shows", {}).get(show, {}).pop("lookup", None)
            self.save()
            self._update_match_label()
            win.destroy()

        ttk.Button(top, text="Search", command=run_search).pack(side="left", padx=(6, 0))
        buttons = ttk.Frame(win, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Use this one", command=use, style="Start.TButton").pack(side="left")
        ttk.Button(buttons, text="No online names for this show", command=clear).pack(side="left", padx=6)
        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: use())
        win.bind("<Return>", lambda e: run_search())
        win.bind("<Escape>", lambda e: win.destroy())
        run_search()

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d')} [{stamp}] {text}\n")
        except OSError:
            pass

    def _on_tk_error(self, *args) -> None:
        text = "".join(traceback.format_exception(*args))
        self.log(text)
        messagebox.showerror(APP_NAME, f"Something went wrong:\n\n{args[1]}")

    def save(self) -> None:
        s = self.settings
        s.update(show=self.v_show.get().strip(), out_root=self.v_out.get().strip(),
                 subfolder=self.v_subfolder.get(), mode=self.v_mode.get(), template=self.v_template.get(),
                 whole_template=self.v_whole_template.get(),
                 disc=self._num(self.v_disc) or 1, episode=self._num(self.v_episode) or 1,
                 min_minutes=self._num(self.v_min, float) or 0, max_minutes=self._num(self.v_max, float) or 0,
                 skip_duplicates=self.v_dups.get(), skip_play_all=self.v_playall_title.get(),
                 use_playall=self.v_use_playall.get(), include_bonus=self.v_bonus.get(),
                 review=self.v_review.get(), eject=self.v_eject.get(), makemkvcon=self.v_exe.get().strip(),
                 mkvmerge=self.v_mkvmerge.get().strip(), ffmpeg=self.v_ffmpeg.get().strip(),
                 previews=self.v_previews.get(), discdb=self.v_discdb.get(),
                 lookup_sources=self._sources(),
                 api_keys={p: self._key(p) for p in self.v_keys},
                 theme="dark" if self.v_dark.get() else "light")
        if drive := self._selected_drive():
            s["drive"] = drive.device
        if s["show"]:
            s.setdefault("shows", {}).setdefault(s["show"], {}).update(
                disc=s["disc"], episode=s["episode"], mode=s["mode"])
        try:
            save_settings(s)
        except OSError as e:
            self.log(f"Couldn't save settings: {e}")

    def _selected_drive(self) -> Drive | None:
        return next((d for d in self.drives if d.label == self.v_drive.get()), None)

    def _offer_libdvdcss(self) -> None:
        """Offer to download libdvdcss when previews are on but it's missing. VideoLAN only has a
        ready-made download for Windows, so macOS and Linux get the install command instead."""
        if (MISSING_FOR_PREVIEWS or not self.v_previews.get()
                or platforms.find_libdvdcss() or getattr(self, "_libdvdcss_dialog", None)):
            return
        self.settings["libdvdcss_asked"] = True
        c = self.colors
        win = self._libdvdcss_dialog = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.configure(background=c["bg"])
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Preview frames need libdvdcss", style="Status.TLabel").pack(anchor="w")
        intro = ("Most commercial DVDs are copy-protected (CSS). To show preview frames for them, "
                 f"{APP_NAME} needs libdvdcss, a free library from VideoLAN. It isn't included with the app.\n\n")
        if platforms.WINDOWS:
            text = intro + ("Download and install it now? It's 46 KB from download.videolan.org and is saved in "
                            f"{APP_NAME}'s settings folder. Ripping works without it.")
        else:
            text = intro + ("Install it with your package manager. It's picked up from the next disc scan.\n\n"
                            f"{platforms.LIBDVDCSS_HINT.replace('; ', chr(10))}\n\nRipping works without it.")
        ttk.Label(frame, wraplength=440, justify="left", text=text).pack(anchor="w", pady=(6, 12))
        status = ttk.Label(frame, text="", style="Muted.TLabel")
        status.pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(8, 0))
        result: dict = {}

        def close() -> None:
            self._libdvdcss_dialog = None
            win.destroy()

        def finish(worker: threading.Thread) -> None:
            if worker.is_alive():
                win.after(100, finish, worker)
                return
            if "path" in result:
                self.log(f"Installed libdvdcss to {result['path']}.")
                close()
                messagebox.showinfo(APP_NAME, "libdvdcss is installed. Protected DVDs will show preview "
                                              "frames from the next disc scan.", parent=self.root)
                return
            status.configure(text="")
            for b in (install_button, later_button):
                b.state(["!disabled"])
            messagebox.showerror(APP_NAME, f"Couldn't install libdvdcss: {result.get('error')}\n\nYou can also "
                                           f"download libdvdcss-2.dll from {thumbs.LIBDVDCSS_DOWNLOAD} and put it "
                                           f"in {platforms.user_lib_dir()}", parent=win)

        def install() -> None:
            for b in (install_button, later_button):
                b.state(["disabled"])
            status.configure(text="Downloading...")

            def work() -> None:
                try:
                    result["path"] = thumbs.install_libdvdcss()
                except Exception as e:  # network, disk or checksum trouble all end up in the message
                    result["error"] = e

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            finish(worker)

        install_button = ttk.Button(buttons, text="Download and install", style="Start.TButton", command=install)
        later_button = ttk.Button(buttons, text="Not now" if platforms.WINDOWS else "OK", command=close)
        if platforms.WINDOWS:
            install_button.pack(side="left")
        later_button.pack(side="left", padx=(8, 0))
        win.protocol("WM_DELETE_WINDOW", close)
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        (install_button if platforms.WINDOWS else later_button).focus_set()

    def _set_still_icon(self, default: bool = False) -> None:
        """The normal icon. default=True also gives it to dialogs opened later."""
        try:
            if platforms.WINDOWS:
                if default:
                    self.root.iconbitmap(default=str(ICON_DIR / "icon.ico"))
                self.root.iconbitmap(str(ICON_DIR / "icon.ico"))
            else:
                self._icon = tk.PhotoImage(file=str(ICON_DIR / "icon.png"))
                self.root.iconphoto(default, self._icon)
        except tk.TclError:
            pass

    def _set_running(self, running: bool) -> None:
        for w in self.inputs:
            if isinstance(w, ttk.Combobox):
                w.state(["disabled"] if running else ["!disabled", "readonly"])
            else:
                w.state(["disabled"] if running else ["!disabled"])
        self.start_btn.state(["disabled"] if running else ["!disabled"])
        self.stop_btn.state(["!disabled"] if running else ["disabled"])
        if not running:
            self._end_review()

    # --- actions ---

    def browse_out(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.v_out.get() or None, title="Where should rips be saved?")
        if folder:
            self.v_out.set(os.path.normpath(folder))

    @staticmethod
    def _ask_program(title: str, pattern: str) -> str:
        # Programs have no extension outside Windows, so there's nothing to filter on there.
        types = [(pattern.split("*")[0].split(".")[0], pattern), ("Programs", "*.exe")] if platforms.WINDOWS else []
        path = filedialog.askopenfilename(title=title, filetypes=types)
        return os.path.normpath(path) if path else ""

    def browse_exe(self) -> None:
        if path := self._ask_program(f"Find {PROGRAM_NAMES['makemkvcon']}", "makemkvcon*.exe"):
            self.v_exe.set(path)
            self.refresh_drives()

    def browse_mkvmerge(self) -> None:
        if path := self._ask_program(f"Find {PROGRAM_NAMES['mkvmerge']} (part of MKVToolNix)", "mkvmerge.exe"):
            self.v_mkvmerge.set(path)

    def browse_ffmpeg(self) -> None:
        if path := self._ask_program(f"Find {PROGRAM_NAMES['ffmpeg']}", "ffmpeg.exe"):
            self.v_ffmpeg.set(path)

    def refresh_drives(self) -> None:
        exe = self.v_exe.get().strip()
        if not os.path.isfile(exe):
            self.log(f"Can't find {PROGRAM_NAMES['makemkvcon']}; set its location at the bottom of the window.")
            return
        self.v_status.set("Looking for drives...")

        def work():
            try:
                self.events.put(("drives", MakeMKV([exe]).drives()))
            except OSError as e:
                self.events.put(("log", f"Couldn't run makemkvcon: {e}"))
                self.events.put(("drives", ([], None)))

        threading.Thread(target=work, daemon=True).start()

    def _build_config(self) -> SessionConfig | None:
        def problem(text: str) -> None:
            messagebox.showwarning(APP_NAME, text)

        show = self.v_show.get().strip()
        template, whole_template = self.v_template.get(), self.v_whole_template.get()
        disc, episode = self._num(self.v_disc), self._num(self.v_episode)
        min_m, max_m = self._num(self.v_min, float), self._num(self.v_max, float)
        exe = self.v_exe.get().strip()
        mkvmerge = self.v_mkvmerge.get().strip()
        drive = self._selected_drive()
        if not show:
            return problem("Enter the show name first (it's used for the folder and file names).")
        if not self.v_out.get().strip():
            return problem("Choose where the rips should be saved.")
        for label, pattern in (("episode", template), ("whole-disc", whole_template)):
            if bad := template_problem(pattern):
                return problem(f"The {label} file name pattern {bad}. Use " +
                               ", ".join(f"{{{p}}}" for p in PLACEHOLDERS) + ".")
        if "{episode" not in template:
            return problem("The episode file name needs {episode} in it, otherwise every episode on a "
                           "disc would get the same name.")
        if not disc or disc < 1 or not episode or episode < 1:
            return problem("Next disc # and next episode # must be whole numbers of 1 or more.")
        if min_m is None or min_m < 0 or max_m is None or max_m < 0:
            return problem("The title length limits must be numbers of minutes (0 or more).")
        if not os.path.isfile(exe):
            return problem(f"Can't find {PROGRAM_NAMES['makemkvcon']}. Set its location at the bottom of the window.")
        if not drive:
            return problem("Pick a drive. If the list is empty, click Refresh.")
        if mkvmerge and not os.path.isfile(mkvmerge):
            return problem("The mkvmerge location at the bottom of the window doesn't exist. "
                           "Fix it, or clear the box if MKVToolNix isn't installed.")
        ffmpeg = self.v_ffmpeg.get().strip()
        if ffmpeg and not os.path.isfile(ffmpeg):
            return problem("The ffmpeg location at the bottom of the window doesn't exist. "
                           "Fix it, or clear the box if ffmpeg isn't installed.")
        if self.v_mode.get() == WHOLE and not mkvmerge and not messagebox.askyesno(
                APP_NAME, "MKVToolNix isn't installed, so discs made of several titles can't be joined "
                          "into one file. Their parts will be saved separately instead.\n\nStart anyway?"):
            return None
        return SessionConfig(command=[exe], drive=drive, out_dir=self._out_dir(), show=show,
                             template=template, disc=disc, episode=episode, min_minutes=min_m,
                             max_minutes=max_m, skip_duplicates=self.v_dups.get(),
                             skip_play_all=self.v_playall_title.get(), review=self.v_review.get(),
                             eject=self.v_eject.get(), mode=self.v_mode.get(), whole_template=whole_template,
                             use_playall=self.v_use_playall.get(), include_bonus=self.v_bonus.get(),
                             mkvmerge=[mkvmerge] if mkvmerge else [],
                             previews=self.v_previews.get() and not MISSING_FOR_PREVIEWS, ffmpeg=ffmpeg,
                             discdb=self.v_discdb.get(), lookup=self._match(),
                             lookup_key=self._key((self._match() or {}).get("provider", "")))

    def start(self) -> None:
        cfg = self._build_config()
        if not cfg:
            return
        if makemkv_gui_running() and not messagebox.askyesno(
                APP_NAME, "The MakeMKV window is open. It can block the drive while GongoAutoRipper is using it.\n\n"
                          "Close MakeMKV first if you can. Start anyway?"):
            return
        self.save()
        self.titles = []
        self.previews.clear()
        self._set_preview_column(cfg.previews and cfg.review)
        self.v_order.set("")
        self._show_titles()
        self.session = RipSession(cfg, lambda kind, payload: self.events.put((kind, payload)))
        self._set_running(True)
        self.session.start()

    def stop(self) -> None:
        if self.session and self.session.running:
            self.v_status.set("Stopping...")
            self.session.stop()

    def on_close(self) -> None:
        if self.session and self.session.running:
            if not messagebox.askyesno(APP_NAME, "A session is running. Stop it and quit?\n\n"
                                                 "A disc that's halfway through will need to be ripped again."):
                return
            self.session.stop()
            self.session.thread.join(timeout=5)
        self.save()
        self.root.destroy()

    # --- titles table and review ---

    def _show_titles(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for t in self.titles:
            if not t.selected:
                name = f"(skip: {t.skip_reason})" if t.skip_reason else "(skip)"
            elif t.kind == "part":
                name = f"part {t.part} of {t.filename}"
            else:
                name = t.filename
            if t.selected and t.warning:
                name += f"  ⚠ {t.warning}"
            tags = []
            if not t.selected:
                tags.append("skipped")
            if t.role == "bonus":
                tags.append("bonus")
            elif t.role in ("full", "full_bonus") and t.selected:
                tags.append("full")
            if t.status == "FAILED":
                tags.append("failed")
            elif t.status in ("Saved", "Ripped"):
                tags.append("saved")
            preview = self.previews.get(t.index)
            self.tree.insert("", "end", iid=str(t.index), tags=tags, image=preview[0] if preview else "",
                             values=("☑" if t.selected else "☐", role_label(t), t.dvd_title or t.index,
                                     fmt_duration(t.duration), t.size, name, t.status))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
            self.tree.see(selected[0])
        all_ticked = bool(self.titles) and all(t.selected for t in self.titles)
        self.check_all_btn.configure(text="Uncheck all" if all_ticked else "Check all")
        made, dropped = rip_summary(self.titles, self.v_disc_mode.get()) if self.titles else ("", "")
        self.v_summary.set(made)
        self.v_dropped.set(dropped)
        nothing_dropped = all(t.selected for t in self.titles)
        self.dropped_label.configure(style="Good.TLabel" if nothing_dropped else "Warn.TLabel")

    def _rename_review(self) -> None:
        r = self.review
        assign_names(self.titles, self.v_disc_mode.get(), self.v_template.get(), self.v_whole_template.get(),
                     self.v_show.get().strip(), r["disc"], r["first_episode"], r.get("names"))
        self._show_titles()

    def _on_tree_click(self, event) -> None:
        column, row = self.tree.identify_column(event.x), self.tree.identify_row(event.y)
        if column == "#0" and row:
            self._open_preview(int(row))
            return
        if not self.review or column != "#1":
            return
        for t in self.titles:
            if str(t.index) == row:
                t.selected = not t.selected
        self._rename_review()

    def review_move(self, step: int) -> None:
        selection = self.tree.selection()
        if not self.review or not selection:
            return
        i = next(i for i, t in enumerate(self.titles) if str(t.index) == selection[0])
        j = i + step
        if 0 <= j < len(self.titles):
            self.titles[i], self.titles[j] = self.titles[j], self.titles[i]
            self._rename_review()

    def review_move_to(self, position: int | None) -> None:
        """Move the selected row to the top (0) or bottom (None) of the list."""
        selection = self.tree.selection()
        if not self.review or not selection:
            return
        i = next(i for i, t in enumerate(self.titles) if str(t.index) == selection[0])
        title = self.titles.pop(i)
        self.titles.insert(len(self.titles) if position is None else position, title)
        self._rename_review()

    def review_check_all(self) -> None:
        """Tick every title, or untick them all if they're already all ticked."""
        if not self.review:
            return
        tick = not all(t.selected for t in self.titles)
        for t in self.titles:
            t.selected = tick
        self._rename_review()

    def _review_mode_changed(self) -> None:
        if not self.review:
            return
        r = self.review
        settings = dataclasses.replace(r["settings"], mode=self.v_disc_mode.get())
        self.titles = build_plan(self.titles, r["order"], settings)
        self.v_order.set(f"Disc {r['disc']} order: {order_note(r['order'], r['note'], settings.mode)}")
        self._rename_review()

    def _end_review(self) -> None:
        self.review = None
        for w in self.review_widgets:
            w.state(["disabled"])
        self.review_hint.configure(text="")

    def review_rip(self) -> None:
        if self.session and self.review:
            choice = {"mode": self.v_disc_mode.get(), "order": [t.index for t in self.titles],
                      "selected": {t.index for t in self.titles if t.selected}}
            self._end_review()
            self.session.reply(choice)

    def review_skip(self) -> None:
        if self.session:
            self._end_review()
            self.session.reply(None)

    def _attention(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.bell()

    # --- events from the session thread ---

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self.log(payload)
        elif kind == "status":
            self.v_status.set(payload)
        elif kind == "progress":
            frac, text = payload
            if frac is None:
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=frac * 1000)
            self.v_progress.set(text)
        elif kind == "titles":
            self.titles = payload["titles"]
            self.v_order.set(f"Disc {payload['disc']} order: "
                             f"{order_note(payload['order'], payload['note'], payload['mode'])}")
            self.v_disc_mode.set(payload["mode"])
            if payload["review"]:
                cfg = self.session.cfg
                self.review = {"disc": payload["disc"], "first_episode": payload["first_episode"],
                               "order": payload["order"], "note": payload["note"],
                               "names": payload.get("names"), "settings": cfg.plan_settings(payload["mode"])}
            self._show_titles()
        elif kind == "thumb":
            self._add_preview(*payload)
        elif kind == "row":
            index, status = payload
            for t in self.titles:
                if t.index == index:
                    t.status = status
            self._show_titles()
        elif kind == "counters":
            disc, episode = payload
            self.v_disc.set(str(disc))
            self.v_episode.set(str(episode))
            self.save()
            self.root.bell()
        elif kind == "ask":
            what, data = payload
            self._attention()
            if what == "review":
                for w in self.review_widgets:
                    w.state(["!disabled"])
                self.review_hint.configure(
                    text="Click a box in the Rip column to include or leave out a title. Select a row and use "
                         "Move up / Move down to change the order. Bonus rows are shaded.")
            elif what == "retry":
                again = messagebox.askyesno(
                    APP_NAME, f"These couldn't be ripped:\n\n{data}\n\nTry them again? If the disc is dirty "
                              "or scratched, you can open the tray, clean it and put it back first.\n\n"
                              "Choose No to move on. Episode numbers stay reserved, so you can rip "
                              "them later by hand.")
                self.session.reply(again)
        elif kind == "fatal":
            self._attention()
            messagebox.showerror(APP_NAME, payload)
        elif kind == "ripping":
            (self.icon_animation.start if payload else self.icon_animation.stop)()
        elif kind == "stopped":
            self.icon_animation.stop()
            self._set_running(False)
            self.v_status.set("Session stopped. Click Start session to continue where you left off.")
        elif kind == "drives":
            self.drives, refusal = payload
            self.drive_box.configure(values=[d.label for d in self.drives])
            preferred = next((d for d in self.drives if d.device == self.settings.get("drive")), None)
            if refusal:
                self.v_drive.set("")
                self.v_status.set("MakeMKV won't run until it's updated or given a new key. "
                                  "Open MakeMKV to fix that, then click Refresh.")
                self.log(f"MakeMKV says: {refusal}")
            elif self.drives:
                self.v_drive.set((preferred or self.drives[0]).label)
                self.v_status.set(f"Ready. Click Start session, then put Disc {self.v_disc.get()} in the drive.")
            else:
                self.v_drive.set("")
                self.v_status.set("No drives found. Check the drive is plugged in, then click Refresh.")


def main() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # Own taskbar entry and icon instead of grouping under pythonw.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
