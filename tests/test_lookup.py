import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gongoautoripper as autorip  # noqa: E402
import lookup  # noqa: E402

from test_app import FAKE, FakeDrive  # noqa: E402


class ContentHashTests(unittest.TestCase):
    def test_matches_the_specification_example(self):
        # TheDiscDb/optical-disc-manifest examples/v1/dvd/basic.json
        sizes = {"VIDEO_TS.IFO": 16384, "VTS_01_0.IFO": 65536, "VTS_01_1.VOB": 1073709056}
        self.assertEqual(lookup.content_hash(sizes), "6EA135E733DB603765376D13F02A2D9D")

    def test_disc_files_picks_the_hashed_dvd_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "VIDEO_TS"
            video.mkdir()
            for name, size in (("VIDEO_TS.IFO", 10), ("VTS_01_1.VOB", 30), ("VIDEO_TS.BUP", 10), ("notes.txt", 5)):
                (video / name).write_bytes(b"x" * size)
            fmt, sizes = lookup.disc_files(Path(tmp))
        self.assertEqual((fmt, sorted(sizes)), ("dvd", ["VIDEO_TS.BUP", "VIDEO_TS.IFO", "VTS_01_1.VOB"]))


class NamingTests(unittest.TestCase):
    TEMPLATE = "{title} - S{season:02}E{season_episode:02} - {episode_title}"

    def test_online_fields_fill_the_name(self):
        name = autorip.format_name(self.TEMPLATE, "Great Chefs", 1, 3, title="Great Chefs", season=1,
                                   season_episode=3, episode_title="Paul Prudhomme")
        self.assertEqual(name, "Great Chefs - S01E03 - Paul Prudhomme.mkv")

    def test_missing_online_fields_are_tidied_away(self):
        name = autorip.format_name("{show} - S{season:02}E{season_episode:02} - {episode_title}", "Great Chefs", 1, 3)
        self.assertEqual(name, "Great Chefs.mkv")
        self.assertEqual(autorip.format_name("{show} ({year})", "Stash", 36, 1), "Stash.mkv")

    def test_unknown_placeholders_are_reported(self):
        self.assertEqual(autorip.template_problem("{show} Disc {disc} Episode {episode:02}"), "")
        self.assertIn("{bogus}", autorip.template_problem("{show} {bogus}"))
        self.assertIn("unmatched", autorip.template_problem("{show"))

    def test_episode_names_follow_the_running_episode_number(self):
        names = autorip.OnlineNames("Battlestar Galactica", "2004",
                                    [lookup.Episode(1, 1, "33"), lookup.Episode(1, 2, "Water")])
        titles = [autorip.Title(i, 2600) for i in range(3)]
        autorip.assign_names(titles, autorip.EPISODES, self.TEMPLATE, "{title} Disc {disc}", "BSG", 1, 2, names)
        self.assertEqual([t.filename for t in titles],
                         ["Battlestar Galactica - S01E02 - Water.mkv", "Battlestar Galactica.mkv",
                          "Battlestar Galactica.mkv"])


def discdb_titles():
    """DVD titles 1-5: two episodes labelled out of disc order, an extra, a trailer, an unlabelled title."""
    titles = [autorip.Title(i, d, dvd_title=i + 1) for i, d in enumerate((2560, 2460, 900, 25, 30))]
    labels = {1: ("Episode", "1", "2", "Water"), 2: ("Episode", "1", "1", "33"),
              3: ("Extra", "", "", "Making Of"), 4: ("Trailer", "", "", "Previews")}
    for t in titles:
        if t.dvd_title in labels:
            t.online_type, t.online_season, t.online_episode, t.online_name = labels[t.dvd_title]
    return titles


class DiscDbPlanTests(unittest.TestCase):
    def test_episodes_follow_the_labels(self):
        planned = autorip.build_plan(discdb_titles(), None, autorip.PlanSettings(autorip.EPISODES))
        autorip.assign_names(planned, autorip.EPISODES, "{show} S{season:02}E{season_episode:02} {episode_title}",
                             "{show} Disc {disc}", "BSG", 1, 1)
        self.assertEqual([(t.dvd_title, t.selected) for t in planned],
                         [(2, True), (1, True), (3, True), (4, True), (5, False)])
        self.assertEqual([t.filename for t in planned[:4]],
                         ["BSG S01E01 33.mkv", "BSG S01E02 Water.mkv", "BSG Disc 1 Bonus 1 - Making Of.mkv",
                          "BSG Disc 1 Bonus 2 - Previews.mkv"])
        self.assertEqual(autorip.role_label(planned[0]), "S1E1 (TheDiscDB)")
        self.assertEqual(planned[4].skip_reason, "not labelled on TheDiscDB")

    def test_whole_disc_mode_ignores_the_labels_for_choosing(self):
        planned = autorip.build_plan(discdb_titles(), None, autorip.PlanSettings(autorip.WHOLE))
        self.assertNotIn("online_episode", [t.role for t in planned])

    def test_session_labels_titles_from_theDiscDB(self):
        match = lookup.DiscMatch("Fake Show", "1999", "tv", "Season 1", "",
                                 {"01": lookup.DiscTitle("Episode", "1", "2", "Second", ""),
                                  "02": lookup.DiscTitle("Episode", "1", "1", "First", ""),
                                  "04": lookup.DiscTitle("Episode", "1", "3", "Third", "")})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "Fake Show"
            drive = FakeDrive(1)
            cfg = autorip.SessionConfig(
                command=[sys.executable, FAKE], drive=autorip.Drive(0, "E:", "fake", ""), out_dir=out,
                show="Fake Show", template="{title} S{season:02}E{season_episode:02} - {episode_title}",
                disc=1, episode=1, min_minutes=10, max_minutes=0, skip_duplicates=True, skip_play_all=True,
                review=False, eject=True, discdb=True)
            session = autorip.RipSession(cfg, lambda kind, payload: None, probe=drive.probe, ejector=drive.eject,
                                         analyzer=lambda letter, available=None: (None, "test"),
                                         identifier=lambda letter: (match, "TheDiscDB: test match"))
            session.poll_interval = session.spinup_delay = 0.01
            session.start()
            self.assertTrue(drive.all_done.wait(60))
            session.stop()
            session.thread.join(10)
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["Fake Show S01E01 - First.mkv", "Fake Show S01E02 - Second.mkv",
                              "Fake Show S01E03 - Third.mkv"])
            # The fake disc's DVD title 2 is 26:10, which TheDiscDB calls episode 1.
            self.assertEqual((out / "Fake Show S01E01 - First.mkv").read_text(), "duration=1570")


class ProviderParsingTests(unittest.TestCase):
    """Each provider's search and episode-list parsing, from trimmed copies of real responses."""

    def fake_get(self, responses):
        def get(url, headers=None, data=None):
            for fragment, body in responses.items():
                if fragment in url:
                    return body
            raise AssertionError(f"unexpected request {url}")
        return mock.patch.object(lookup, "_get_json", side_effect=get)

    def test_tvmaze(self):
        with self.fake_get({"search/shows": [{"show": {"id": 166, "name": "Battlestar Galactica",
                                                       "premiered": "2004-10-18", "network": {"name": "Syfy"}}}],
                            "shows/166/episodes": [{"season": 1, "number": 2, "name": "Water"},
                                                   {"season": 1, "number": 1, "name": "33"},
                                                   {"season": 0, "number": None, "name": "Special"}]}):
            match = lookup.search("TVmaze", "", "battlestar")[0]
            eps = lookup.episodes(match, "")
        self.assertEqual(match.label, "Battlestar Galactica (2004) - TV series - Syfy")
        self.assertEqual([(e.season, e.number, e.title) for e in eps], [(1, 1, "33"), (1, 2, "Water")])

    def test_tmdb(self):
        with self.fake_get({"search/tv": {"results": [{"id": 1972, "name": "Battlestar Galactica",
                                                        "first_air_date": "2004-10-18"}]},
                            "search/movie": {"results": [{"id": 69315, "title": "Battlestar Galactica: Razor",
                                                           "release_date": "2007-11-24"}]},
                            "/tv/1972/season/1": {"episodes": [{"episode_number": 1, "name": "33"}]},
                            "/tv/1972?": {"seasons": [{"season_number": 0}, {"season_number": 1}]}}):
            matches = lookup.search("TMDb", "abc123", "battlestar")
            eps = lookup.episodes(matches[0], "abc123")
        self.assertEqual([(m.kind, m.title, m.year) for m in matches],
                         [("tv", "Battlestar Galactica", "2004"), ("movie", "Battlestar Galactica: Razor", "2007")])
        self.assertEqual([(e.season, e.number, e.title) for e in eps], [(1, 1, "33")])

    def test_omdb(self):
        with self.fake_get({"&s=": {"Response": "True", "Search": [
                                {"Title": "Battlestar Galactica", "Year": "2004–2009", "imdbID": "tt0407362",
                                 "Type": "series"},
                                {"Title": "Some Game", "Year": "2003", "imdbID": "tt1", "Type": "game"}]},
                            "&Season=1": {"Episodes": [{"Title": "33", "Episode": "1"}]},
                            "&i=tt0407362": {"totalSeasons": "1"}}):
            matches = lookup.search("OMDb", "abc123", "battlestar")
            eps = lookup.episodes(matches[0], "abc123")
        self.assertEqual([(m.id, m.kind, m.year) for m in matches], [("tt0407362", "tv", "2004")])
        self.assertEqual([(e.season, e.number, e.title) for e in eps], [(1, 1, "33")])

    def test_omdb_bad_key_is_reported(self):
        with self.fake_get({"&s=": {"Response": "False", "Error": "Invalid API key!"}}):
            with self.assertRaises(lookup.OnlineError):
                lookup.search("OMDb", "wrong", "battlestar")

    def test_discdb_response(self):
        body = {"data": {"mediaItems": {"nodes": [{"title": "24", "year": 2001, "type": "Series", "releases": [
            {"title": "Season 1", "discs": [{"disc": {"name": None, "contentHash": "ABC", "titles": [
                {"sourceFile": "01", "duration": "0:42:36", "itemType": "Episode", "season": "1", "episode": "1",
                 "description": "12:00 A.M.-1:00 A.M."},
                {"sourceFile": "05", "duration": "0:01:00", "itemType": "", "season": "", "episode": "",
                 "description": ""}]}}]}]}]}}}
        with self.fake_get({"graphql": body}):
            match = lookup.discdb_lookup("ABC")
        self.assertEqual((match.title, match.year, match.kind, match.release), ("24", "2001", "tv", "Season 1"))
        self.assertEqual(list(match.titles), ["01"])


if __name__ == "__main__":
    unittest.main()
