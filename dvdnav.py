"""Work out the order a DVD plays its titles in when you pick "Play All".

Reads the disc's IFO files and imitates a DVD player: it runs the disc's
navigation commands from first play to the main menu, then tries each menu
button and records which titles play. The button that plays the most is taken
to be "Play All". The command semantics follow libdvdnav's vm/decoder.c.

Usage:  python dvdnav.py E:
"""

from __future__ import annotations

import random
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

SECTOR = 2048

FP, VMGM, VTSM, TITLE = "first-play", "main menu", "title menu", "title"

LINK_SUB = {1: "LinkTopC", 2: "LinkNextC", 3: "LinkPrevC", 5: "LinkTopPG", 6: "LinkNextPG",
            7: "LinkPrevPG", 9: "LinkTopPGC", 10: "LinkNextPGC", 11: "LinkPrevPGC",
            12: "LinkGoUpPGC", 13: "LinkTailPGC", 16: "LinkRSM"}


def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _bcd(x):
    return (x >> 4) * 10 + (x & 0x0F)


def _bcd_time(b, o) -> float:
    h, m, s, f = b[o:o + 4]
    return _bcd(h) * 3600 + _bcd(m) * 60 + _bcd(s) + _bcd(f & 0x3F) / (25 if f >> 6 == 1 else 30)


def _bits(cmd: int, start: int, count: int) -> int:
    """libdvdnav's vm_getbits: `count` bits ending at bit `start` (63 = first bit)."""
    return (cmd >> (start - count + 1)) & ((1 << count) - 1)


# ---------------------------------------------------------------------------
# IFO parsing
# ---------------------------------------------------------------------------

@dataclass
class Cell:
    block_mode: int
    block_type: int
    still: int
    cmd_nr: int
    duration: float
    first_sector: int
    last_sector: int
    vob_id: int = 0
    cell_id: int = 0


@dataclass
class PGC:
    category: int
    duration: float
    programs: list[int]
    cells: list[Cell]
    pre: list[int]
    post: list[int]
    cell_cmds: list[int]
    next: int
    prev: int
    goup: int


def _parse_pgc(b: bytes, o: int, category: int = 0) -> PGC:
    nprog, ncell = b[o + 2], b[o + 3]
    cmd_off, map_off, cpb_off, cpos_off = (_u16(b, o + 0xE4), _u16(b, o + 0xE6),
                                           _u16(b, o + 0xE8), _u16(b, o + 0xEA))
    pre, post, cell_cmds = [], [], []
    if cmd_off:
        c = o + cmd_off
        npre, npost, ncellc = _u16(b, c), _u16(b, c + 2), _u16(b, c + 4)
        cmds = [int.from_bytes(b[c + 8 + 8 * i: c + 16 + 8 * i], "big") for i in range(npre + npost + ncellc)]
        pre, post, cell_cmds = cmds[:npre], cmds[npre:npre + npost], cmds[npre + npost:]
    programs = [b[o + map_off + i] for i in range(nprog)] if map_off else []
    cells = []
    for i in range(ncell if cpb_off else 0):
        p = o + cpb_off + 24 * i
        pos = o + cpos_off + 4 * i
        cells.append(Cell(block_mode=b[p] >> 6, block_type=(b[p] >> 4) & 3, still=b[p + 2],
                          cmd_nr=b[p + 3], duration=_bcd_time(b, p + 4),
                          first_sector=_u32(b, p + 8), last_sector=_u32(b, p + 20),
                          vob_id=_u16(b, pos) if cpos_off else 0, cell_id=b[pos + 3] if cpos_off else 0))
    return PGC(category, _bcd_time(b, o + 4), programs, cells, pre, post, cell_cmds,
               _u16(b, o + 0x9C), _u16(b, o + 0x9E), _u16(b, o + 0xA0))


def _parse_pgcit(b: bytes, start: int) -> list[PGC]:
    return [_parse_pgc(b, start + _u32(b, start + 8 + 8 * i + 4), b[start + 8 + 8 * i])
            for i in range(_u16(b, start))]


def _parse_menus(b: bytes, sector: int) -> list[PGC]:
    """Menu PGCs from a PGCI_UT, preferring the English language unit."""
    if not sector:
        return []
    start = sector * SECTOR
    units = [(bytes(b[start + 8 + 8 * i: start + 10 + 8 * i]), _u32(b, start + 8 + 8 * i + 4))
             for i in range(_u16(b, start))]
    if not units:
        return []
    offset = next((off for lang, off in units if lang == b"en"), units[0][1])
    return _parse_pgcit(b, start + offset)


@dataclass
class TitleEntry:
    number: int
    vts: int
    vts_ttn: int
    chapters: int
    vts_sector: int = 0  # where the title set starts on the disc


@dataclass
class VTS:
    pgcs: list[PGC]
    ptt: list[list[tuple[int, int]]]
    menus: list[PGC]


@dataclass
class Button:
    number: int
    command: int


class Disc:
    def __init__(self, root: str | Path):
        root = Path(root if str(root).endswith(("\\", "/")) else f"{root}\\")
        self.dir = root if root.name.upper() == "VIDEO_TS" else root / "VIDEO_TS"
        vmg = (self.dir / "VIDEO_TS.IFO").read_bytes()
        if vmg[:12] != b"DVDVIDEO-VMG":
            raise ValueError("not a DVD-Video disc")
        tt = _u32(vmg, 0xC4) * SECTOR
        self.titles = [TitleEntry(i + 1, vmg[tt + 8 + 12 * i + 6], vmg[tt + 8 + 12 * i + 7],
                                  _u16(vmg, tt + 8 + 12 * i + 2), _u32(vmg, tt + 8 + 12 * i + 8))
                       for i in range(_u16(vmg, tt))]
        fp = _u32(vmg, 0x84)
        self.first_play = _parse_pgc(vmg, fp) if fp else None
        self.vmgm = _parse_menus(vmg, _u32(vmg, 0xC8))
        self._vts: dict[int, VTS] = {}
        self._buttons: dict[tuple, tuple[list[Button], int]] = {}

    def vts(self, n: int) -> VTS:
        if n not in self._vts:
            b = (self.dir / f"VTS_{n:02}_0.IFO").read_bytes()
            ptt_start = _u32(b, 0xC8) * SECTOR
            count, end = _u16(b, ptt_start), _u32(b, ptt_start + 4)
            offsets = [_u32(b, ptt_start + 8 + 4 * i) for i in range(count)] + [end + 1]
            ptt = [[(_u16(b, p), _u16(b, p + 2)) for p in range(ptt_start + offsets[i], ptt_start + offsets[i + 1], 4)]
                   for i in range(count)]
            self._vts[n] = VTS(_parse_pgcit(b, _u32(b, 0xCC) * SECTOR), ptt, _parse_menus(b, _u32(b, 0xD0)))
        return self._vts[n]

    def title_number(self, vts: int, vts_ttn: int) -> int:
        return next((t.number for t in self.titles if t.vts == vts and t.vts_ttn == vts_ttn), 0)

    def title_for_pgc(self, vts: int, pgcn: int) -> int:
        for ttn, parts in enumerate(self.vts(vts).ptt, start=1):
            if any(p == pgcn for p, _ in parts):
                return self.title_number(vts, ttn)
        return 0

    def buttons(self, domain: str, vtsn: int, cell: Cell) -> tuple[list[Button], int]:
        """Buttons in a menu cell, read from the NAV packs of the menu VOB, plus the forced-activate button."""
        key = (domain, vtsn, cell.first_sector)
        if key not in self._buttons:
            vob = self.dir / ("VIDEO_TS.VOB" if domain == VMGM else f"VTS_{vtsn:02}_0.VOB")
            self._buttons[key] = _read_buttons(vob, cell.first_sector, cell.last_sector)
        return self._buttons[key]


def _read_buttons(vob: Path, first: int, last: int) -> tuple[list[Button], int]:
    try:
        f = open(vob, "rb")
    except OSError:
        return [], 0
    with f:
        for sector in range(first, min(last, first + 4000) + 1):
            f.seek(sector * SECTOR)
            s = f.read(SECTOR)
            if len(s) < SECTOR:
                break
            if s[:4] != b"\x00\x00\x01\xba" or s[0x26:0x2A] != b"\x00\x00\x01\xbf" or s[0x2C] != 0:
                continue
            hli = 0x2D + 0x60  # PCI data starts at 0x2D; highlight info is 0x60 into it
            if _u16(s, hli) & 3 == 0:
                continue
            count, forced = s[hli + 17], s[hli + 21]
            buttons = [Button(i + 1, int.from_bytes(s[hli + 46 + 18 * i + 10: hli + 46 + 18 * i + 18], "big"))
                       for i in range(min(count, 36))]
            return buttons, forced
    return [], 0


# ---------------------------------------------------------------------------
# The imitation player
# ---------------------------------------------------------------------------

@dataclass
class Play:
    """A stretch of title playback: cells first_cell..last_cell of one PGC."""
    title: int
    vts: int
    pgcn: int
    first_cell: int
    last_cell: int
    duration: float


@dataclass
class Link:
    kind: str
    data1: int = 0
    data2: int = 0
    data3: int = 0
    button: int = 0


class StepLimit(Exception):
    pass


@dataclass
class VM:
    disc: Disc
    gprm: list[int] = field(default_factory=lambda: [0] * 16)
    sprm: list[int] = field(default_factory=lambda: [0] * 24)
    domain: str = FP
    vtsn: int = 0
    pgcn: int = 1
    pgn: int = 1
    celln: int = 0
    rsm: tuple | None = None
    plays: list[Play] = field(default_factory=list)
    steps: int = 0
    max_steps: int = 200_000

    def __post_init__(self):
        if not any(self.sprm):
            s = self.sprm
            s[0] = s[16] = s[18] = (ord("e") << 8) | ord("n")
            s[1], s[2], s[3], s[4], s[5], s[7] = 15, 62, 1, 1, 1, 1
            s[8], s[12], s[13], s[14], s[15], s[20] = 1 << 10, (ord("U") << 8) | ord("S"), 15, 0x100, 0x7CFC, 1
        self.rng = random.Random(0)

    def clone(self) -> VM:
        vm = VM(self.disc, list(self.gprm), list(self.sprm), self.domain, self.vtsn, self.pgcn,
                self.pgn, self.celln, self.rsm, [], self.steps, self.max_steps)
        vm.rng = random.Random(self.steps)
        return vm

    # --- where we are ---

    def _pgc_list(self) -> list[PGC]:
        if self.domain == FP:
            return [self.disc.first_play] if self.disc.first_play else []
        if self.domain == VMGM:
            return self.disc.vmgm
        vts = self.disc.vts(self.vtsn)
        return vts.menus if self.domain == VTSM else vts.pgcs

    @property
    def pgc(self) -> PGC | None:
        pgcs = self._pgc_list()
        return pgcs[self.pgcn - 1] if 0 < self.pgcn <= len(pgcs) else None

    # --- registers and command evaluation (libdvdnav decoder.c) ---

    def _reg(self, r: int) -> int:
        return self.sprm[r & 0x1F] if r & 0x80 else self.gprm[r & 0x0F]

    def _reg_or_data(self, c: int, imm: int, start: int) -> int:
        return _bits(c, start, 16) if imm else self._reg(_bits(c, start - 8, 8))

    @staticmethod
    def _compare(op: int, a: int, b: int) -> bool:
        return {1: lambda: bool(a & b), 2: lambda: a == b, 3: lambda: a != b, 4: lambda: a >= b,
                5: lambda: a > b, 6: lambda: a <= b, 7: lambda: a < b}[op]()

    def _if1(self, c):
        op = _bits(c, 54, 3)
        return not op or self._compare(op, self._reg(_bits(c, 39, 8)), self._reg_or_data(c, _bits(c, 55, 1), 31))

    def _if2(self, c):
        op = _bits(c, 54, 3)
        return not op or self._compare(op, self._reg(_bits(c, 15, 8)), self._reg(_bits(c, 7, 8)))

    def _if3(self, c):
        op = _bits(c, 54, 3)
        return not op or self._compare(op, self._reg(_bits(c, 43, 4)), self._reg_or_data(c, _bits(c, 55, 1), 15))

    def _if4(self, c):
        op = _bits(c, 54, 3)
        return not op or self._compare(op, self._reg(_bits(c, 51, 4)), self._reg_or_data(c, _bits(c, 55, 1), 31))

    def _if5(self, c):
        op = _bits(c, 54, 3)
        if not op:
            return True
        if _bits(c, 60, 1):
            return self._compare(op, self._reg(_bits(c, 31, 8)), self._reg(_bits(c, 23, 8)))
        return self._compare(op, self._reg(_bits(c, 39, 8)), self._reg_or_data(c, _bits(c, 55, 1), 31))

    def _set_op(self, op: int, reg: int, reg2: int, data: int) -> None:
        g = self.gprm
        if op == 1:
            g[reg] = data
        elif op == 2:
            g[reg2], g[reg] = g[reg], data
        elif op == 3:
            g[reg] = min(g[reg] + data, 0xFFFF)
        elif op == 4:
            g[reg] = max(g[reg] - data, 0)
        elif op == 5:
            g[reg] = min(g[reg] * data, 0xFFFF)
        elif op == 6:
            g[reg] = g[reg] // data if data else 0xFFFF
        elif op == 7:
            g[reg] = g[reg] % data if data else 0xFFFF
        elif op == 8:
            g[reg] = 1 + int(data * self.rng.random())
        elif op == 9:
            g[reg] &= data
        elif op == 10:
            g[reg] |= data
        elif op == 11:
            g[reg] ^= data

    def _set1(self, c, cond):
        if cond:
            self._set_op(_bits(c, 59, 4), _bits(c, 35, 4), _bits(c, 19, 4), self._reg_or_data(c, _bits(c, 60, 1), 31))

    def _set2(self, c, cond):
        if cond:
            self._set_op(_bits(c, 59, 4), _bits(c, 51, 4), _bits(c, 35, 4), self._reg_or_data(c, _bits(c, 60, 1), 47))

    def _system_set(self, c, cond):
        op = _bits(c, 59, 4)
        imm = _bits(c, 60, 1)
        if op == 1:
            for i in range(1, 4):
                if _bits(c, 63 - (2 + i) * 8, 1):
                    start = 47 - i * 8
                    data = _bits(c, start - 1, 7) if imm else self.gprm[_bits(c, start - 4, 4)]
                    if cond:
                        self.sprm[i] = data
        elif op == 2 and cond:
            self.sprm[9], self.sprm[10] = self._reg_or_data(c, imm, 47), _bits(c, 23, 8)
        elif op == 3 and cond:
            self.gprm[_bits(c, 19, 4)] = self._reg_or_data(c, imm, 47)
        elif op == 6 and cond:
            self.sprm[8] = self._reg_or_data(c, imm, 31)

    def _link_sub(self, c) -> Link | None:
        op = _bits(c, 4, 5)
        return Link(LINK_SUB[op], button=_bits(c, 15, 6)) if op in LINK_SUB else None

    def _link_ins(self, c) -> Link | None:
        op = _bits(c, 51, 4)
        if op == 1:
            return self._link_sub(c)
        if op == 4:
            return Link("LinkPGCN", _bits(c, 14, 15))
        if op == 5:
            return Link("LinkPTTN", _bits(c, 9, 10), button=_bits(c, 15, 6))
        if op == 6:
            return Link("LinkPGN", _bits(c, 6, 7), button=_bits(c, 15, 6))
        if op == 7:
            return Link("LinkCN", _bits(c, 7, 8), button=_bits(c, 15, 6))
        return None

    def _jump_ins(self, c) -> Link | None:
        op = _bits(c, 51, 4)
        if op == 1:
            return Link("Exit")
        if op == 2:
            return Link("JumpTT", _bits(c, 22, 7))
        if op == 3:
            return Link("JumpVTS_TT", _bits(c, 22, 7))
        if op == 5:
            return Link("JumpVTS_PTT", _bits(c, 22, 7), _bits(c, 41, 10))
        target = _bits(c, 23, 2)
        if op == 6:
            return [Link("JumpSS_FP"), Link("JumpSS_VMGM_MENU", _bits(c, 19, 4)),
                    Link("JumpSS_VTSM", _bits(c, 31, 8), _bits(c, 39, 8), _bits(c, 19, 4)),
                    Link("JumpSS_VMGM_PGC", _bits(c, 46, 15))][target]
        if op == 8:
            rsm = _bits(c, 31, 8)
            return [Link("CallSS_FP", rsm), Link("CallSS_VMGM_MENU", _bits(c, 19, 4), rsm),
                    Link("CallSS_VTSM", _bits(c, 19, 4), rsm),
                    Link("CallSS_VMGM_PGC", _bits(c, 46, 15), rsm)][target]
        return None

    def _eval(self, c: int):
        """One command: None, ("goto", line), ("break",) or a Link."""
        kind = _bits(c, 63, 3)
        if kind == 0:
            if not self._if1(c):
                return None
            sub = _bits(c, 51, 4)
            if sub == 1:
                return ("goto", _bits(c, 7, 8))
            if sub == 2:
                return ("break",)
            if sub == 3:
                self.sprm[13] = _bits(c, 11, 4)
                return ("goto", _bits(c, 7, 8))
            return None
        if kind == 1:
            if _bits(c, 60, 1):
                cond, link = self._if2(c), self._jump_ins(c)
            else:
                cond, link = self._if1(c), self._link_ins(c)
            return link if cond else None
        if kind == 2:
            cond = self._if2(c)
            self._system_set(c, cond)
            return self._link_ins(c) if cond and _bits(c, 51, 4) else None
        if kind == 3:
            cond = self._if3(c)
            self._set1(c, cond)
            return self._link_ins(c) if cond and _bits(c, 51, 4) else None
        if kind == 4:
            self._set2(c, True)
            return self._link_sub(c) if self._if4(c) else None
        if kind == 5:
            if self._if5(c):
                self._set2(c, True)
                return self._link_sub(c)
            return None
        if kind == 6:
            cond = self._if5(c)
            self._set2(c, cond)
            return self._link_sub(c)
        return None

    def _eval_list(self, cmds: list[int]) -> Link | None:
        i = total = 0
        while i < len(cmds) and total < 10_000:
            self.steps += 1
            r = self._eval(cmds[i])
            total += 1
            if isinstance(r, Link):
                return r
            if r and r[0] == "break":
                return None
            i = r[1] - 1 if r and r[0] == "goto" and r[1] else i + 1
        return None

    # --- playback ---

    def run(self, action=("play_pgc",)):
        """Advance until the player stops or waits at a menu. Returns ("menu", buttons) or ("stop", None)."""
        try:
            while True:
                self.steps += 1
                if self.steps > self.max_steps:
                    raise StepLimit
                action = self._step(action)
                if action[0] in ("wait", "stop"):
                    return ("menu", action[1]) if action[0] == "wait" else ("stop", None)
        except StepLimit:
            return "limit", None

    def press(self, button: Button):
        self.sprm[8] = button.number << 10
        link = self._eval_list([button.command])
        return self.run(("link", link)) if link else ("menu", None)

    def _start_program(self, pgn: int):
        pgc = self.pgc
        if not pgc.programs or pgn > len(pgc.programs):
            return ("pgc_post",)
        self.pgn = pgn
        self.celln = pgc.programs[pgn - 1]
        return ("play_cell",)

    def _step(self, action):
        what = action[0]
        if what in ("play_pgc", "play_pgc_pg"):
            pgc = self.pgc
            if pgc is None:
                return ("stop", None)
            link = self._eval_list(pgc.pre)
            if link:
                return ("link", link)
            return self._start_program(action[1] if what == "play_pgc_pg" else 1)
        if what == "play_cell":
            return self._play_cell()
        if what == "cell_post":
            return self._cell_post()
        if what == "pgc_post":
            pgc = self.pgc
            link = self._eval_list(pgc.post)
            if link:
                return ("link", link)
            if pgc.next:
                self.pgcn = pgc.next
                return ("play_pgc",)
            return ("stop", None)
        if what == "link":
            return self._link(action[1])
        raise ValueError(action)

    def _play_cell(self):
        pgc = self.pgc
        if self.celln < 1 or self.celln > len(pgc.cells):
            return ("pgc_post",)
        cell = pgc.cells[self.celln - 1]
        if cell.block_type == 1 and cell.block_mode > 1:  # only the first angle of an angle block
            return ("cell_post",)
        if self.domain == TITLE:
            last = self.plays[-1] if self.plays else None
            if last and (last.vts, last.pgcn, last.last_cell) == (self.vtsn, self.pgcn, self.celln - 1):
                last.last_cell = self.celln
                last.duration += cell.duration
            else:
                self.plays.append(Play(self.disc.title_for_pgc(self.vtsn, self.pgcn) or self.sprm[4],
                                       self.vtsn, self.pgcn, self.celln, self.celln, cell.duration))
        elif self.domain in (VMGM, VTSM):
            buttons, forced = self.disc.buttons(self.domain, self.vtsn, cell)
            if buttons:
                if forced and forced <= len(buttons):
                    return ("link", self._button_link(buttons[forced - 1]))
                return ("wait", buttons)
            if cell.still == 0xFF:
                return ("wait", [])
        return ("cell_post",)

    def _button_link(self, button: Button):
        self.sprm[8] = button.number << 10
        return self._eval_list([button.command]) or Link("Exit")

    def _cell_post(self):
        pgc = self.pgc
        cell = pgc.cells[self.celln - 1]
        if cell.cmd_nr and cell.cmd_nr <= len(pgc.cell_cmds):
            link = self._eval_list([pgc.cell_cmds[cell.cmd_nr - 1]])
            if link:
                return ("link", link)
        if cell.block_type == 1 and cell.block_mode in (1, 2):
            while self.celln < len(pgc.cells) and pgc.cells[self.celln - 1].block_mode != 3:
                self.celln += 1
        self.celln += 1
        if pgc.programs and self.pgn < len(pgc.programs) and self.celln >= pgc.programs[self.pgn]:
            self.pgn += 1
        return ("play_cell",)

    def _save_rsm(self, cell: int) -> None:
        if self.domain == TITLE:
            self.rsm = (self.vtsn, self.pgcn, cell or self.celln, tuple(self.sprm[4:9]))

    def _set_vts_ptt(self, vts: int, ttn: int, part: int):
        parts = self.disc.vts(vts).ptt
        if not (0 < ttn <= len(parts)) or not (0 < part <= len(parts[ttn - 1])):
            return ("stop", None)
        pgcn, pgn = parts[ttn - 1][part - 1]
        self.domain, self.vtsn, self.pgcn = TITLE, vts, pgcn
        self.sprm[4], self.sprm[5], self.sprm[6], self.sprm[7] = self.disc.title_number(vts, ttn), ttn, pgcn, part
        return ("play_pgc_pg", pgn)

    def _set_menu(self, menu_id: int):
        for i, pgc in enumerate(self._pgc_list(), start=1):
            if pgc.category & 0x80 and pgc.category & 0x0F == menu_id:
                self.pgcn = i
                return ("play_pgc",)
        return ("stop", None)

    def _link(self, link: Link):
        k = link.kind
        if link.button:
            self.sprm[8] = link.button << 10
        pgc = self.pgc
        if k == "LinkTopC":
            return ("play_cell",)
        if k == "LinkNextC":
            self.celln += 1
            return ("play_cell",)
        if k == "LinkPrevC":
            self.celln = max(1, self.celln - 1)
            return ("play_cell",)
        if k == "LinkTopPG":
            return self._start_program(self.pgn)
        if k == "LinkNextPG":
            return self._start_program(self.pgn + 1)
        if k == "LinkPrevPG":
            return self._start_program(max(1, self.pgn - 1))
        if k == "LinkTopPGC":
            return ("play_pgc",)
        if k in ("LinkNextPGC", "LinkPrevPGC", "LinkGoUpPGC"):
            target = {"LinkNextPGC": pgc.next, "LinkPrevPGC": pgc.prev, "LinkGoUpPGC": pgc.goup}[k]
            if not target:
                return ("stop", None)
            self.pgcn = target
            return ("play_pgc",)
        if k == "LinkTailPGC":
            return ("pgc_post",)
        if k == "LinkRSM":
            if not self.rsm:
                return ("stop", None)
            self.vtsn, self.pgcn, cell, regs = self.rsm
            self.domain = TITLE
            self.sprm[4:9] = list(regs)
            self.celln = cell
            return ("play_cell",)
        if k == "LinkPGCN":
            self.pgcn = link.data1
            return ("play_pgc",)
        if k == "LinkPTTN":
            return self._set_vts_ptt(self.vtsn, self.sprm[5], link.data1) if self.domain == TITLE else ("stop", None)
        if k == "LinkPGN":
            return self._start_program(link.data1)
        if k == "LinkCN":
            self.celln = link.data1
            return ("play_cell",)
        if k == "Exit":
            return ("stop", None)
        if k == "JumpTT":
            if not 0 < link.data1 <= len(self.disc.titles):
                return ("stop", None)
            t = self.disc.titles[link.data1 - 1]
            return self._set_vts_ptt(t.vts, t.vts_ttn, 1)
        if k == "JumpVTS_TT":
            return self._set_vts_ptt(self.vtsn, link.data1, 1)
        if k == "JumpVTS_PTT":
            return self._set_vts_ptt(self.vtsn, link.data1, link.data2)
        if k.startswith("CallSS"):
            self._save_rsm(link.data1 if k == "CallSS_FP" else link.data2)
        if k in ("JumpSS_FP", "CallSS_FP"):
            self.domain, self.pgcn = FP, 1
            return ("play_pgc",)
        if k in ("JumpSS_VMGM_MENU", "CallSS_VMGM_MENU"):
            self.domain = VMGM
            return self._set_menu(link.data1)
        if k in ("JumpSS_VMGM_PGC", "CallSS_VMGM_PGC"):
            self.domain, self.pgcn = VMGM, link.data1
            return ("play_pgc",)
        if k == "JumpSS_VTSM":
            if link.data1:
                self.vtsn = link.data1
            self.domain = VTSM
            self.sprm[5] = link.data2
            self.sprm[4] = self.disc.title_number(self.vtsn, link.data2)
            return self._set_menu(link.data3)
        if k == "CallSS_VTSM":
            self.domain = VTSM
            return self._set_menu(link.data1)
        return ("stop", None)


# ---------------------------------------------------------------------------
# Finding "Play All"
# ---------------------------------------------------------------------------

@dataclass
class PlayAllResult:
    plays: list[Play]           # what plays after pressing the button, in order
    intro: list[Play]           # what played before the main menu appeared
    path: list[int]             # buttons pressed, e.g. [2] or [3, 1] through a sub-menu
    note: str = ""


def _distinct(plays: list[Play]) -> list[Play]:
    seen, out = set(), []
    for p in plays:
        key = (p.vts, p.pgcn, p.first_cell)
        if key in seen:
            break  # the disc started repeating itself
        seen.add(key)
        out.append(p)
    return out


def find_play_all(disc: Disc, max_depth: int = 3, max_steps: int = 200_000) -> PlayAllResult | None:
    vm = VM(disc, max_steps=max_steps)
    status, buttons = vm.run()
    intro = list(vm.plays)
    if status != "menu" or not buttons:
        return PlayAllResult(_distinct(vm.plays), [], [], "the disc never stopped at a menu") if vm.plays else None

    best: PlayAllResult | None = None
    frontier = [(vm, buttons, [])]
    seen_menus = {(vm.domain, vm.vtsn, vm.pgcn, vm.celln)}
    while frontier:
        base, buttons, path = frontier.pop(0)
        for button in buttons:
            trial = base.clone()
            status, next_buttons = trial.press(button)
            plays = _distinct(trial.plays)
            if plays:
                total = sum(p.duration for p in plays)
                if best is None or total > sum(p.duration for p in best.plays):
                    best = PlayAllResult(plays, intro, path + [button.number])
            elif status == "menu" and next_buttons and len(path) + 1 < max_depth:
                where = (trial.domain, trial.vtsn, trial.pgcn, trial.celln)
                if where not in seen_menus:
                    seen_menus.add(where)
                    frontier.append((trial, next_buttons, path + [button.number]))
    return best


@dataclass
class DiscOrder:
    """How each DVD title (numbered as in the disc's title table) relates to Play All."""
    playall: list[int]          # titles Play All plays, in order (empty if no Play All was found)
    containers: set[int]        # Play All titles that hold other titles (a single "play all" title)
    inside: dict[int, int]      # titles whose video sits inside a container -> position in Play All
    duplicates: set[int]        # titles that repeat video Play All already plays
    intro: set[int]             # titles that play before the menu (warnings, logos)
    note: str = ""
    # For one-file rips: titles that together play every piece of video on the disc once.
    complete: list[int] = field(default_factory=list)
    complete_extras: set[int] = field(default_factory=set)   # added after the main playback order
    left_out: dict[int, str] = field(default_factory=dict)   # why each other title isn't needed
    repeats: dict[int, float] = field(default_factory=dict)  # seconds of already-covered video a chosen title repeats
    complete_note: str = ""


MIN_NEW_SECONDS = 1.0  # anything shorter is blank padding, not content


def complete_cover(title_cells: dict[int, list[tuple]], cell_seconds: dict[tuple, float],
                   playall: list[int], sequence: list[tuple], intro: set[int],
                   available: set[int] | None = None):
    """Choose titles that between them play every stored piece of video exactly once.

    DVD titles are playlists of stored pieces (cells), so two titles share video only by
    pointing at the same piece. Start from whichever playback order covers the most video
    (the menu's Play All chain, or a single title), then add the titles that bring in
    video not yet covered, preferring ones that repeat the least. Pre-menu titles are left
    for the user to add. Returns (titles in order, the extras among them, why each other
    title was left out, seconds of repeated video in the extras).

    available: the titles the ripper can actually rip. MakeMKV quietly leaves out some
    titles (for example ones it takes for copy-protection decoys), so only those are chosen.
    """
    def seconds(keys) -> float:
        return sum(cell_seconds.get(k, 0.0) for k in set(keys))

    if available is not None:
        title_cells = {n: keys for n, keys in title_cells.items() if n in available}
        if not all(n in available for n in playall):
            playall, sequence = [], []
    titles = {n: keys for n, keys in title_cells.items() if keys and n not in intro}
    candidates = dict(titles)
    if len(playall) >= 2:
        candidates[0] = sequence  # 0 stands for the menu's Play All chain
    if not candidates:
        return [], set(), {}, {}
    # With a Play All, start from it or from a title that holds all of it and more (Play All
    # with extras inserted); never from an unrelated bonus title, which would scatter Play All.
    if playall:
        playall_cells = set(sequence)
        candidates = {c: keys for c, keys in candidates.items()
                      if c == 0 or c in playall or playall_cells <= set(keys)}
    base = max(candidates, key=lambda c: (round(seconds(candidates[c]), 1), c == 0 or c in playall, -c))
    chosen = list(playall) if base == 0 else [base]
    covered = set(candidates[base])
    extras: list[int] = []
    repeats: dict[int, float] = {}
    while True:
        best, best_score = None, 0.0
        for n, keys in titles.items():
            if n in chosen or n in extras:
                continue
            new = seconds(k for k in keys if k not in covered)
            score = new - seconds(k for k in keys if k in covered)
            if new >= MIN_NEW_SECONDS and (best is None or score > best_score):
                best, best_score = n, score
        if best is None:
            break
        repeated = seconds(k for k in titles[best] if k in covered)
        if repeated >= 1:
            repeats[best] = repeated
        extras.append(best)
        covered |= set(titles[best])
    chosen += sorted(extras)

    left_out = {}
    for n, keys in titles.items():
        if n in chosen:
            continue
        owner = max(chosen, key=lambda c: seconds(set(keys) & set(title_cells.get(c, []))))
        if seconds(set(keys) & set(title_cells.get(owner, []))) > 0:
            left_out[n] = f"repeats video from title {owner}"
        else:
            left_out[n] = "no video of its own (under a second)"
    return chosen, set(extras), left_out, repeats


def _cell_seconds(disc: Disc) -> dict[tuple, float]:
    seconds = {}
    for t in disc.titles:
        for pgc in disc.vts(t.vts).pgcs:
            for c in pgc.cells:
                seconds[(t.vts, c.vob_id, c.cell_id)] = c.duration
    return seconds


def _title_cells(disc: Disc, number: int) -> list[tuple[int, int, int]]:
    t = disc.titles[number - 1]
    vts = disc.vts(t.vts)
    if not 0 < t.vts_ttn <= len(vts.ptt):
        return []
    keys = []
    for pgcn in dict.fromkeys(p for p, _ in vts.ptt[t.vts_ttn - 1]):
        if 0 < pgcn <= len(vts.pgcs):
            keys += [(t.vts, c.vob_id, c.cell_id) for c in vts.pgcs[pgcn - 1].cells]
    return keys


def analyze(root: str | Path, available: set[int] | None = None) -> DiscOrder | None:
    """Find Play All, sort every title into Play All / inside it / duplicate / intro / bonus,
    and work out which titles cover the whole disc once (for one-file rips), choosing only
    from `available` titles when given (the ones the ripper lists)."""
    disc = Disc(root)
    result = find_play_all(disc)
    cells_of = {t.number: _title_cells(disc, t.number) for t in disc.titles}
    playall: list[int] = []
    sequence: list[tuple[int, int, int]] = []
    for p in result.plays if result else []:
        if p.title and p.title not in playall:
            playall.append(p.title)
        cells = disc.vts(p.vts).pgcs[p.pgcn - 1].cells[p.first_cell - 1:p.last_cell]
        sequence += [(p.vts, c.vob_id, c.cell_id) for c in cells]
    position: dict[tuple, int] = {}
    for i, key in enumerate(sequence):
        position.setdefault(key, i)

    containers: set[int] = set()
    inside: dict[int, int] = {}
    duplicates: set[int] = set()
    for t in disc.titles:
        keys = cells_of[t.number]
        if t.number in playall or not keys or not all(k in position for k in keys):
            continue
        owner = next((p for p in playall if set(keys) <= set(cells_of[p])), None)
        if owner and len(set(cells_of[owner])) > len(set(keys)):
            containers.add(owner)
            inside[t.number] = position[keys[0]]
        else:
            duplicates.add(t.number)
    intro = {p.title for p in result.intro if p.title and p.title not in playall} if result else set()
    if len(playall) < 2 and not containers:
        playall, sequence, inside, duplicates = [], [], {}, set()  # one ordinary title playing isn't a Play All
        note = "disc order (no Play All found in the DVD menus)"
    else:
        bonus = len(disc.titles) - len(playall) - len(inside) - len(duplicates) - len(intro)
        if containers:
            note = (f"from the disc's Play All title, which holds {len(inside)} separate title(s)"
                    f"{f'; {bonus} bonus title(s) not in Play All' if bonus else ''}")
        else:
            note = (f"from the disc's Play All menu ({len(playall)} titles in order)"
                    f"{f'; {bonus} bonus title(s) not in Play All' if bonus else ''}")

    complete, extras, left_out, repeats = complete_cover(cells_of, _cell_seconds(disc), playall, sequence, intro,
                                                         available)
    if not complete and not playall:
        return None
    names = (f"Play All ({len(playall)} titles)" if len(playall) >= 2 and complete[:len(playall)] == playall
             else f"title {complete[0]}") if complete else ""
    complete_note = (f"the checked titles are everything on the disc, each piece once, in play order: {names}"
                     f"{f' + {len(extras)} more' if extras else ''}"
                     f"{'; some repeat a little video (see the table)' if repeats else ''}")
    return DiscOrder(playall, containers, inside, duplicates, intro, note,
                     complete, extras, left_out, repeats, complete_note)


def main(argv: list[str]) -> int:
    disc = Disc(argv[1] if len(argv) > 1 else "E:")
    result = find_play_all(disc)
    if not result:
        print("Couldn't find anything that plays from the menus.")
        return 1
    if result.intro:
        print("Before the menu:", ", ".join(f"title {p.title}" for p in result.intro))
    print(f"Play All = button path {result.path}  {result.note}")
    total = 0.0
    for i, p in enumerate(result.plays, start=1):
        total += p.duration
        m, s = divmod(int(p.duration), 60)
        print(f"{i:3}. title {p.title:3}  (VTS {p.vts}, PGC {p.pgcn}, cells {p.first_cell}-{p.last_cell})  {m}:{s:02}")
    print(f"{len(result.plays)} plays, {int(total) // 60}:{int(total) % 60:02} total")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
