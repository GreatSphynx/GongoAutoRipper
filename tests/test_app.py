import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gongoautoripper as autorip  # noqa: E402
import dvdnav  # noqa: E402
from dvdnav import DiscOrder  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_makemkvcon.py"))
FAKE_MKVMERGE = str(Path(__file__).with_name("fake_mkvmerge.py"))


class FakeDrive:
    """Plays a stack of discs: each eject empties the drive, then the next disc goes in."""

    def __init__(self, discs: int):
        self.waiting = list(range(2, discs + 1))
        self.current: int | None = 1
        self.ejected = 0
        self.all_done = threading.Event()

    def probe(self, letter):
        if self.current is None:
            if self.waiting:
                self.current = self.waiting.pop(0)
            return None
        return (1000 + self.current, f"DISC{self.current}")

    def eject(self, letter):
        self.current = None
        self.ejected += 1
        if not self.waiting:
            self.all_done.set()
        return True


class ParsingTests(unittest.TestCase):
    def test_drive_line_from_real_makemkv(self):
        lines = ['DRV:0,2,999,1,"BD-RE HL-DT-ST BD-RE BP60NB10 1.02 SIM03P9894146","STASH36","E:"',
                 'DRV:1,256,999,0,"","",""']
        drives = autorip.parse_drives(lines)
        self.assertEqual(len(drives), 1)
        self.assertEqual((drives[0].index, drives[0].device, drives[0].disc_name), (0, "E:", "STASH36"))

    def test_license_error_from_real_makemkv(self):
        line = ('MSG:5021,131332,1,"This application version is too old.  Please download the latest '
                'version at http://www.makemkv.com/ or enter a registration key to continue using the '
                'current version.","This application version is too old.","http://www.makemkv.com/"')
        info = autorip.parse_info([line])
        self.assertIn("too old", autorip.license_error(info.messages))

    def test_dvd_title_number_from_real_makemkv(self):
        info = autorip.parse_info(['TINFO:77,9,0,"0:12:39"', 'TINFO:77,24,0,"78"'])
        self.assertEqual(info.titles[0].dvd_title, 78)


class FilterTests(unittest.TestCase):
    def test_dvd_titles_sharing_segment_numbers_are_not_duplicates(self):
        # From a real DVD: every clip is segment "1" of its own title.
        titles = [autorip.Title(1, 262, size_bytes=200_900_000, segments="1"),
                  autorip.Title(3, 61, size_bytes=51_270_000, segments="1"),
                  autorip.Title(8, 61, size_bytes=50_750_000, segments="1")]
        autorip.apply_filters(titles, 0, 0, True, False)
        self.assertTrue(all(t.selected for t in titles))

    def test_filters_and_names(self):
        titles = [autorip.Title(0, 1560, size_bytes=900, segments="1-3"),
                  autorip.Title(1, 1570, size_bytes=910, segments="4-6"),
                  autorip.Title(2, 1550, size_bytes=890, segments="8-10"),
                  autorip.Title(3, 4680, size_bytes=2700, segments="1-10"),
                  autorip.Title(4, 1560, size_bytes=900, segments="1-3"),
                  autorip.Title(5, 120, size_bytes=50, segments="11")]
        autorip.apply_filters(titles, 10, 0, True, True)
        self.assertEqual([t.selected for t in titles], [True, True, True, False, False, False])
        nxt = autorip.assign_names(titles, autorip.EPISODES, autorip.DEFAULT_TEMPLATE,
                                   autorip.DEFAULT_WHOLE_TEMPLATE, "Great Chefs", 2, 4)
        self.assertEqual(nxt, 7)
        self.assertEqual([t.filename for t in titles[:3]],
                         ["Great Chefs Disc 2 Episode 4.mkv", "Great Chefs Disc 2 Episode 5.mkv",
                          "Great Chefs Disc 2 Episode 6.mkv"])

    def test_padding_and_bad_characters(self):
        name = autorip.format_name("{show} - S01E{episode:02}", "What: Now?", 1, 3)
        self.assertEqual(name, "What Now - S01E03.mkv")


def stash_like_titles():
    """MakeMKV indexes 0-5 for DVD titles 1-6: intro, two Play All items, two bonus, one too short."""
    return [autorip.Title(i, d, dvd_title=i + 1) for i, d in enumerate((6, 262, 151, 300, 700, 10))]


STASH_ORDER = DiscOrder(playall=[3, 2], containers=set(), inside={}, duplicates=set(), intro={1})


class PlanTests(unittest.TestCase):
    def test_whole_disc_is_play_all_order_then_bonus(self):
        titles = autorip.build_plan(stash_like_titles(), STASH_ORDER, autorip.PlanSettings(autorip.WHOLE))
        chosen = [t.dvd_title for t in titles if t.selected]
        self.assertEqual(chosen, [3, 2, 4, 5])
        # The pre-menu title is listed first, unticked, so it can be added at the start.
        self.assertEqual((titles[0].dvd_title, titles[0].role, titles[0].selected), (1, "intro", False))
        self.assertEqual([t.role for t in titles if t.selected], ["playall", "playall", "bonus", "bonus"])
        skipped = {t.dvd_title: t.skip_reason for t in titles if not t.selected}
        self.assertEqual(skipped[1], "plays before the menu")
        self.assertIn("under 30 s", skipped[6])
        autorip.assign_names(titles, autorip.WHOLE, autorip.DEFAULT_TEMPLATE, autorip.DEFAULT_WHOLE_TEMPLATE,
                             "Stash", 36, 1)
        self.assertEqual([t.part_name for t in titles if t.selected],
                         ["Part 01.mkv", "Part 02.mkv", "Bonus 03.mkv", "Bonus 04.mkv"])
        self.assertEqual({t.filename for t in titles if t.selected}, {"Stash Disc 36.mkv"})

    def test_ticked_intro_is_named_as_intro(self):
        titles = autorip.build_plan(stash_like_titles(), STASH_ORDER, autorip.PlanSettings(autorip.WHOLE))
        titles[0].selected = True
        autorip.assign_names(titles, autorip.WHOLE, autorip.DEFAULT_TEMPLATE, autorip.DEFAULT_WHOLE_TEMPLATE,
                             "Stash", 36, 1)
        self.assertEqual([t.part_name for t in titles if t.selected][:2], ["Intro 01.mkv", "Part 02.mkv"])
        autorip.assign_names(titles, autorip.EPISODES, autorip.DEFAULT_TEMPLATE, autorip.DEFAULT_WHOLE_TEMPLATE,
                             "Stash", 36, 1)
        self.assertEqual(titles[0].filename, "Stash Disc 36 Intro 1.mkv")
        self.assertEqual(titles[1].filename, "Stash Disc 36 Episode 1.mkv")

    def test_bonus_titles_can_be_turned_off(self):
        settings = autorip.PlanSettings(autorip.WHOLE, include_bonus=False)
        titles = autorip.build_plan(stash_like_titles(), STASH_ORDER, settings)
        self.assertEqual([t.dvd_title for t in titles if t.selected], [3, 2])

    def test_episodes_follow_the_play_all_title(self):
        # Title 4 is a Play All title holding titles 3, 1 and 2, in that order.
        titles = [autorip.Title(i, d, dvd_title=i + 1) for i, d in enumerate((1560, 1570, 1550, 4680, 200))]
        order = DiscOrder(playall=[4], containers={4}, inside={3: 0, 1: 9, 2: 20}, duplicates=set(), intro=set())
        titles = autorip.build_plan(titles, order, autorip.PlanSettings(autorip.EPISODES))
        autorip.assign_names(titles, autorip.EPISODES, autorip.DEFAULT_TEMPLATE, autorip.DEFAULT_WHOLE_TEMPLATE,
                             "Great Chefs", 1, 1)
        named = [(t.dvd_title, t.filename) for t in titles if t.selected]
        self.assertEqual(named, [(3, "Great Chefs Disc 1 Episode 1.mkv"), (1, "Great Chefs Disc 1 Episode 2.mkv"),
                                 (2, "Great Chefs Disc 1 Episode 3.mkv"), (5, "Great Chefs Disc 1 Bonus 1.mkv")])


class CompleteCoverTests(unittest.TestCase):
    """dvdnav.complete_cover on simplified versions of real discs. Cells are named by letter."""

    @staticmethod
    def cover(titles, seconds, playall=(), intro=()):
        sequence = [c for n in playall for c in titles[n]]
        return dvdnav.complete_cover(titles, seconds, list(playall), sequence, set(intro))

    def test_stash31_picks_the_fullest_title_and_the_separate_short(self):
        pieces = ["p1", "p2", "p3", "p4"]
        titles = {3: pieces,                                    # menu Play All, 70 min
                  2: ["p1", "m1", "p2", "p3", "m2", "p4"],      # Play All with making-ofs inserted
                  4: ["p2", "m1", "p3", "m2"],                  # behind-the-scenes section
                  33: ["m1"], 34: ["m2"], 42: ["s1", "s2"], 1: ["blank"]}
        seconds = {**{c: 600 for c in pieces}, "m1": 380, "m2": 33, "s1": 200, "s2": 195, "blank": 0.7}
        chosen, extras_chosen, left_out, repeats = self.cover(titles, seconds, playall=[3])
        self.assertEqual(chosen, [2, 42])
        self.assertEqual(extras_chosen, {42})
        self.assertEqual(repeats, {})
        self.assertEqual(left_out[3], "repeats video from title 2")
        self.assertEqual(left_out[4], "repeats video from title 2")
        self.assertIn("no video of its own", left_out[1])

    def test_stash36_keeps_the_menu_chain_and_adds_bonus(self):
        titles = {38: ["c1"], 3: ["a"], 39: ["c2"], 4: ["b"], 72: ["x"], 78: ["y"], 1: ["fbi"]}
        seconds = {"c1": 9, "a": 151, "c2": 9, "b": 61, "x": 177, "y": 759, "fbi": 6}
        chosen, extras_chosen, _, _ = self.cover(titles, seconds, playall=[38, 3, 39, 4], intro=[1])
        self.assertEqual(chosen, [38, 3, 39, 4, 72, 78])
        self.assertEqual(extras_chosen, {72, 78})

    def test_stash33_only_picks_titles_makemkv_can_rip(self):
        # Title 2 is Play All with two pieces added, but MakeMKV doesn't list it; those two
        # pieces also exist on their own as titles 33 and 34.
        pieces = [f"p{i}" for i in range(6)]
        titles = {2: pieces[:4] + ["x1"] + pieces[4:5] + ["x2"] + pieces[5:], 3: pieces,
                  33: ["x1"], 34: ["x2"], 35: ["bonus"], 4: ["p1", "x1", "x2"]}
        seconds = {**{p: 500 for p in pieces}, "x1": 281, "x2": 129, "bonus": 327}
        chosen, extras, _, repeats = dvdnav.complete_cover(titles, seconds, [3], pieces, set(),
                                                           available={3, 4, 33, 34, 35})
        self.assertEqual(chosen, [3, 33, 34, 35])
        self.assertEqual((extras, repeats), ({33, 34, 35}, {}))
        # Without the restriction title 2 would win, which MakeMKV can't rip.
        self.assertEqual(dvdnav.complete_cover(titles, seconds, [3], pieces, set())[0], [2, 35])

    def test_title_with_new_and_repeated_video_is_kept_and_flagged(self):
        titles = {2: ["a", "b", "c"], 5: ["a", "d"]}
        seconds = {"a": 30, "b": 600, "c": 600, "d": 60}
        chosen, _, _, repeats = self.cover(titles, seconds)
        self.assertEqual(chosen, [2, 5])
        self.assertEqual(repeats, {5: 30})

    def test_whole_disc_plan_ticks_the_cover_in_order(self):
        titles = [autorip.Title(i, d, dvd_title=n) for i, (n, d) in enumerate([(2, 4990), (3, 4204), (4, 1460),
                                                                              (33, 381), (42, 395)])]
        order = DiscOrder(playall=[3], containers={3}, inside={}, duplicates=set(), intro=set(),
                          complete=[2, 42], complete_extras={42},
                          left_out={3: "repeats video from title 2", 4: "repeats video from title 2",
                                    33: "repeats video from title 2"},
                          complete_note="everything once")
        planned = autorip.build_plan(titles, order, autorip.PlanSettings(autorip.WHOLE))
        self.assertEqual([(t.dvd_title, t.selected) for t in planned],
                         [(2, True), (42, True), (3, False), (4, False), (33, False)])
        self.assertEqual([autorip.role_label(t) for t in planned[:2]], ["Whole disc 1", "Whole disc 2 (extra)"])
        self.assertEqual(planned[2].skip_reason, "repeats video from title 2")
        # Episodes mode on the same disc still uses the Play All classification.
        episodes = autorip.build_plan(titles, order, autorip.PlanSettings(autorip.EPISODES))
        self.assertEqual(next(t for t in episodes if t.dvd_title == 3).role, "container")


@unittest.skipUnless(autorip.find_ffmpeg() and autorip.find_mkvmerge(), "ffmpeg and MKVToolNix needed")
class ChapterJoinTests(unittest.TestCase):
    def test_parts_keep_their_own_chapters(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder, ffmpeg = Path(tmp.name), autorip.find_ffmpeg()
        meta = folder / "meta.txt"
        meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Intro\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=3000\ntitle=Main\n")
        parts = []
        for name, chapters in (("Part 01", True), ("Bonus 02", False)):
            out = folder / f"{name}.mkv"
            cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=3:s=160x120:r=25",
                   "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3"]
            if chapters:
                cmd += ["-i", str(meta), "-map_chapters", "2"]
            cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "mpeg2video", "-c:a", "ac3", str(out)]
            subprocess.run(cmd, check=True)
            parts.append(out)
        joined = folder / "joined.mkv"
        ok, output = autorip.join_mkvs([autorip.find_mkvmerge()], parts, joined)
        self.assertTrue(ok, output)
        marks = autorip.chapter_layout([autorip.find_mkvmerge()], [joined])
        self.assertEqual([name for _, name in marks],
                         ["joined - Part 01 - Intro", "joined - Part 01 - Main", "joined - Bonus 02"])
        self.assertEqual([round(start, 1) for start, _ in marks], [0.0, 1.0, 3.0])


class RipSummaryTests(unittest.TestCase):
    def test_episodes_with_dropped_titles_grouped_by_reason(self):
        titles = [autorip.Title(i, 1500) for i in range(9)]
        for t in titles[:3]:
            t.kind = "episode"
        titles[3].kind = "bonus"
        for t, reason in zip(titles[4:], ("repeats video from title 2", "duplicate of title 1",
                                          "shorter than 10 min", "plays before the menu", "")):
            t.selected, t.skip_reason = False, reason
        made, dropped = autorip.rip_summary(titles, autorip.EPISODES)
        self.assertEqual(made, "This rip will create 4 separate files (3 episodes, 1 bonus).")
        self.assertEqual(dropped, "5 titles are dropped: 2× video already in the ticked titles, "
                                  "1× shorter than the minimum length, 1× plays before the menu, 1× unticked by hand.")

    def test_whole_disc(self):
        titles = [autorip.Title(i, 600) for i in range(3)]
        titles[2].selected, titles[2].skip_reason = False, "repeats video from title 2"
        self.assertEqual(autorip.rip_summary(titles, autorip.WHOLE),
                         ("This rip will create 1 merged file from 2 titles.",
                          "1 title is dropped: 1× video already in the ticked titles."))

    def test_nothing_ticked(self):
        titles = [autorip.Title(0, 600, selected=False)]
        self.assertTrue(autorip.rip_summary(titles, autorip.EPISODES)[0].startswith("Nothing is ticked"))


class OneRunTests(unittest.TestCase):
    def test_many_small_titles_rip_in_one_run(self):
        # Stash: 77 wanted titles; only a 1.5 MB warning isn't needed.
        titles = [autorip.Title(i, 60, size_bytes=50_000_000) for i in range(77)]
        titles.append(autorip.Title(77, 6, size_bytes=1_500_000))
        self.assertTrue(autorip.rip_in_one_run(titles[:77], titles, scan_seconds=150))

    def test_big_unneeded_play_all_title_is_skipped(self):
        # Four episodes plus a 3 GB play-all title that would be copied for nothing.
        episodes = [autorip.Title(i, 1560, size_bytes=750_000_000) for i in range(4)]
        titles = episodes + [autorip.Title(4, 6240, size_bytes=3_000_000_000)]
        self.assertFalse(autorip.rip_in_one_run(episodes, titles, scan_seconds=20))

    def test_lots_of_unneeded_video_rips_titles_separately(self):
        # Stash 33: 4 wanted titles, ~3 GB of repeated pieces, a 2-minute disc opening. One pass
        # would only win on a fast disc, so the slow-disc assumption keeps the rips separate.
        wanted = [autorip.Title(i, 600, size_bytes=800_000_000) for i in range(4)]
        titles = wanted + [autorip.Title(4 + i, 100, size_bytes=100_000_000) for i in range(30)]
        self.assertTrue(autorip.rip_in_one_run(wanted, titles, 128, bytes_per_second=8_000_000))
        self.assertFalse(autorip.rip_in_one_run(wanted, titles, 128))

    def test_single_title_never_needs_a_full_run(self):
        titles = [autorip.Title(0, 1560), autorip.Title(1, 6)]
        self.assertFalse(autorip.rip_in_one_run(titles[:1], titles, scan_seconds=150))


class PreviewTests(unittest.TestCase):
    @staticmethod
    def sector(pes_flags: int = 0x81, nav: bool = False) -> bytes:
        s = bytearray(2048)
        s[0:4] = b"\x00\x00\x01\xba"
        if nav:
            s[0x26:0x2A] = b"\x00\x00\x01\xbf"
        else:
            s[14:18] = b"\x00\x00\x01\xe0"
            s[20] = pes_flags
        return bytes(s)

    def test_scrambling_bits(self):
        import thumbs
        self.assertFalse(thumbs.is_scrambled(self.sector(nav=True) + self.sector(0x81)))
        self.assertTrue(thumbs.is_scrambled(self.sector(nav=True) + self.sector(0x91)))  # bits 5-4 = 01

    def test_decoding_starts_at_a_navigation_pack(self):
        import thumbs
        data = self.sector() + self.sector() + self.sector(nav=True) + self.sector()
        self.assertEqual(len(thumbs.from_first_vobu(data)), 2 * 2048)


@unittest.skipUnless(autorip.find_ffmpeg(), "ffmpeg isn't installed")
class AudioPaddingTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = autorip.find_ffmpeg()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def clip(self, video_seconds, audio_seconds) -> Path:
        import subprocess
        out = self.folder / "Part 01.mkv"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=d={video_seconds}:s=160x120:r=25",
                        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={audio_seconds}",
                        "-c:v", "mpeg2video", "-c:a", "ac3", "-b:a", "128k", str(out)], check=True)
        return out

    def audio_end(self, part) -> float:
        last = autorip._ffprobe(self.ffmpeg, ["-select_streams", "a:0", "-show_entries", "packet=pts_time,duration_time",
                                              "-of", "csv=p=0"], part).split()[-1]
        pts, length = map(float, last.split(",")[:2])  # newer ffprobe can add an empty field
        return pts + length

    def test_card_with_short_audio_is_padded_to_the_video_length(self):
        part = self.clip(video_seconds=4, audio_seconds=1)
        self.assertLess(self.audio_end(part), 1.2)
        note = autorip.pad_short_audio(self.ffmpeg, part)
        self.assertTrue(note.startswith("padded"), note)
        self.assertAlmostEqual(self.audio_end(part), 4, delta=0.1)
        codecs = autorip._ffprobe(self.ffmpeg, ["-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1"],
                                  part).split()
        self.assertEqual(codecs, ["mpeg2video", "ac3"])
        self.assertEqual([p.name for p in self.folder.iterdir()], ["Part 01.mkv"])

    def test_part_with_full_audio_is_left_alone(self):
        part = self.clip(video_seconds=3, audio_seconds=3)
        before = part.read_bytes()
        self.assertEqual(autorip.pad_short_audio(self.ffmpeg, part), "")
        self.assertEqual(part.read_bytes(), before)


def cmd(hex_bytes: str) -> int:
    return int.from_bytes(bytes.fromhex(hex_bytes.replace(" ", "")), "big")


class DvdCommandTests(unittest.TestCase):
    """Commands copied from the Stash 36 disc."""

    def setUp(self):
        self.vm = dvdnav.VM(disc=None)

    def test_conditional_goto(self):
        self.vm.gprm[13] = 1
        self.assertEqual(self.vm._eval(cmd("00 c1 00 0d 00 01 00 04")), ("goto", 4))
        self.vm.gprm[13] = 0
        self.assertIsNone(self.vm._eval(cmd("00 c1 00 0d 00 01 00 04")))

    def test_copy_title_number_into_register(self):
        self.vm.sprm[4] = 38
        self.vm._eval(cmd("61 00 00 0f 00 84 00 00"))
        self.assertEqual(self.vm.gprm[15], 38)

    def test_jumps(self):
        link = self.vm._eval(cmd("30 06 00 01 06 83 00 00"))
        self.assertEqual((link.kind, link.data1, link.data2, link.data3), ("JumpSS_VTSM", 6, 1, 3))
        link = self.vm._eval(cmd("30 06 00 03 00 c0 00 00"))
        self.assertEqual((link.kind, link.data1), ("JumpSS_VMGM_PGC", 3))
        link = self.vm._eval(cmd("20 04 00 00 00 00 00 e7"))
        self.assertEqual((link.kind, link.data1), ("LinkPGCN", 231))


class SessionTests(unittest.TestCase):
    def run_session(self, discs, mode=autorip.EPISODES, order=None, review_choice=None, fail_once=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "Great Chefs"
        self.call_log = Path(tmp.name) / "calls.txt"
        os.environ["FAKE_CALL_LOG"] = str(self.call_log)
        self.addCleanup(os.environ.pop, "FAKE_CALL_LOG", None)
        if fail_once:
            os.environ["FAKE_FAIL_ONCE"] = str(Path(tmp.name) / "failed.marker")
            self.addCleanup(os.environ.pop, "FAKE_FAIL_ONCE", None)
        drive = FakeDrive(discs)
        events = []
        shown = {}
        cfg = autorip.SessionConfig(
            command=[sys.executable, FAKE], drive=autorip.Drive(0, "E:", "fake", ""), out_dir=out,
            show="Great Chefs", template=autorip.DEFAULT_TEMPLATE, disc=1, episode=1, min_minutes=10,
            max_minutes=0, skip_duplicates=True, skip_play_all=True, review=review_choice is not None,
            eject=True, mode=mode, mkvmerge=[sys.executable, FAKE_MKVMERGE])

        def emit(kind, payload):
            events.append((kind, payload))
            if kind == "titles":
                shown.update(payload)
            if kind == "ask":
                if payload[0] == "review":
                    session.reply({"mode": mode, "order": [t.index for t in shown["titles"]],
                                   "selected": review_choice})
                else:
                    session.reply(True)

        session = autorip.RipSession(cfg, emit, probe=drive.probe, ejector=drive.eject,
                                     analyzer=lambda letter, available=None: (order, "test order"))
        session.poll_interval = session.spinup_delay = 0.01
        session.start()
        self.assertTrue(drive.all_done.wait(60), "session did not finish the discs")
        session.stop()
        session.thread.join(10)
        self.assertNotIn("fatal", [k for k, _ in events], events)
        return out, session, events

    def test_two_discs_number_episodes_across_discs(self):
        out, session, _ = self.run_session(discs=2)
        files = sorted(p.name for p in out.iterdir())
        self.assertEqual(files, [f"Great Chefs Disc {d} Episode {e}.mkv"
                                 for d, e in ((1, 1), (1, 2), (1, 3), (2, 4), (2, 5), (2, 6))])
        # Episode order follows title order: the 26:10 title is the second episode.
        self.assertEqual((out / "Great Chefs Disc 2 Episode 5.mkv").read_text(), "duration=1570")
        self.assertEqual((session.disc, session.episode), (3, 7))

    def test_each_disc_is_ripped_in_one_makemkv_run(self):
        _, _, events = self.run_session(discs=2)
        self.assertEqual(self.call_log.read_text().split(), ["all", "all"])
        logged = [p for k, p in events if k == "log"]
        self.assertFalse([line for line in logged if "was added" in line], "per-title scan chatter was logged")

    def test_review_choice_limits_titles(self):
        out, session, _ = self.run_session(discs=1, review_choice={0, 3})
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["Great Chefs Disc 1 Episode 1.mkv", "Great Chefs Disc 1 Episode 2.mkv"])
        self.assertEqual((out / "Great Chefs Disc 1 Episode 2.mkv").read_text(), "duration=1550")
        self.assertEqual(session.episode, 3)

    def test_failed_title_is_retried(self):
        out, _, events = self.run_session(discs=1, fail_once=True)
        self.assertIn(("ask", ("retry", "Great Chefs Disc 1 Episode 2.mkv")), events)
        self.assertEqual(len(list(out.glob("*.mkv"))), 3)
        # One pass for the disc, then only the failed title again.
        self.assertEqual(self.call_log.read_text().split(), ["all", "1"])

    def test_episodes_in_play_all_order_with_bonus(self):
        # DVD titles 1-6 on the fake disc; title 5 is a Play All title holding 4, 2 and 1.
        order = DiscOrder(playall=[5], containers={5}, inside={4: 0, 2: 10, 1: 20}, duplicates={6}, intro=set())
        out, session, _ = self.run_session(discs=1, order=order)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["Great Chefs Disc 1 Bonus 1.mkv", "Great Chefs Disc 1 Episode 1.mkv",
                          "Great Chefs Disc 1 Episode 2.mkv", "Great Chefs Disc 1 Episode 3.mkv"])
        self.assertEqual([(out / f"Great Chefs Disc 1 Episode {e}.mkv").read_text() for e in (1, 2, 3)],
                         ["duration=1550", "duration=1570", "duration=1560"])
        self.assertEqual((out / "Great Chefs Disc 1 Bonus 1.mkv").read_text(), "duration=120")
        self.assertEqual(session.episode, 4)

    def test_whole_disc_joins_play_all_then_bonus(self):
        order = DiscOrder(playall=[4, 1, 2], containers=set(), inside={}, duplicates={6}, intro=set())
        out, session, _ = self.run_session(discs=1, mode=autorip.WHOLE, order=order)
        self.assertEqual([p.name for p in out.iterdir()], ["Great Chefs Disc 1.mkv"])
        self.assertEqual((out / "Great Chefs Disc 1.mkv").read_text(),
                         "duration=1550|duration=1560|duration=1570|duration=120|duration=4680")
        self.assertEqual((session.disc, session.episode), (2, 1))

    def test_whole_disc_keeps_parts_without_mkvtoolnix(self):
        order = DiscOrder(playall=[4, 1], containers=set(), inside={}, duplicates=set(), intro=set())
        tmp_out, _, events = self.run_session_without_mkvmerge(order)
        folder = tmp_out / "Great Chefs Disc 1 (parts)"
        # Play All is titles 4 and 1; titles 2, 3, 5 and 6 aren't in it, so they're bonus parts.
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["Bonus 03.mkv", "Bonus 04.mkv", "Bonus 05.mkv", "Bonus 06.mkv",
                          "Part 01.mkv", "Part 02.mkv"])
        self.assertEqual((folder / "Part 01.mkv").read_text(), "duration=1550")

    def run_session_without_mkvmerge(self, order):
        original = autorip.SessionConfig.__init__

        def no_mkvmerge(cfg, *args, **kwargs):
            original(cfg, *args, **kwargs)
            cfg.mkvmerge = []

        autorip.SessionConfig.__init__ = no_mkvmerge
        self.addCleanup(setattr, autorip.SessionConfig, "__init__", original)
        return self.run_session(discs=1, mode=autorip.WHOLE, order=order)


if __name__ == "__main__":
    unittest.main()
