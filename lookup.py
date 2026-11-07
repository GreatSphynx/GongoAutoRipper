"""Online names for discs, shows and episodes.

- TheDiscDB: identifies the exact disc from its file sizes and labels each title
  (episode, extra, trailer...). No key needed. https://thediscdb.com
- TMDb: movies and TV with episode names. Free API key. https://www.themoviedb.org
- OMDb: IMDb-based movies and TV. Free API key, 1,000 requests a day. https://www.omdbapi.com
- TVmaze: TV shows and episode names. No key needed. https://www.tvmaze.com
"""

from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "GongoAutoRipper/1.0"
TIMEOUT = 20
PROVIDERS = ("TMDb", "OMDb", "TVmaze")
NEEDS_KEY = {"TMDb", "OMDb"}


class OnlineError(Exception):
    pass


def _get_json(url: str, headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise OnlineError("the API key was refused") from e
        raise OnlineError(f"the service answered {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OnlineError(f"couldn't reach the service ({e})") from e
    except ValueError as e:
        raise OnlineError("the service sent something unreadable") from e


# ---------------------------------------------------------------------------
# Show / movie search and episode lists
# ---------------------------------------------------------------------------

@dataclass
class Match:
    provider: str
    id: str
    title: str
    year: str = ""
    kind: str = ""          # "tv" or "movie"
    note: str = ""          # extra detail for the picker, e.g. the network

    @property
    def label(self) -> str:
        kind = {"tv": "TV series", "movie": "Movie"}.get(self.kind, self.kind)
        return " - ".join(x for x in (f"{self.title} ({self.year})" if self.year else self.title, kind, self.note) if x)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "id": self.id, "title": self.title, "year": self.year, "kind": self.kind}

    @classmethod
    def from_dict(cls, d: dict) -> Match:
        return cls(d["provider"], str(d["id"]), d.get("title", ""), d.get("year", ""), d.get("kind", ""))


@dataclass
class Episode:
    season: int
    number: int
    title: str


def _year(date: str | None) -> str:
    return (date or "")[:4]


def search(provider: str, key: str, query: str) -> list[Match]:
    query = query.strip()
    if not query:
        return []
    q = urllib.parse.quote(query)
    if provider == "TVmaze":
        results = _get_json(f"https://api.tvmaze.com/search/shows?q={q}")
        return [Match("TVmaze", str(r["show"]["id"]), r["show"]["name"], _year(r["show"].get("premiered")), "tv",
                      ((r["show"].get("network") or r["show"].get("webChannel") or {}).get("name") or ""))
                for r in results]
    if provider == "TMDb":
        matches = []
        for kind, path, title_key, date_key in (("tv", "tv", "name", "first_air_date"),
                                                ("movie", "movie", "title", "release_date")):
            data = _tmdb(key, f"/search/{path}?query={q}")
            matches += [Match("TMDb", str(r["id"]), r.get(title_key, ""), _year(r.get(date_key)), kind)
                        for r in data.get("results", [])[:10]]
        return matches
    if provider == "OMDb":
        data = _get_json(f"https://www.omdbapi.com/?apikey={urllib.parse.quote(key)}&s={q}")
        if data.get("Response") == "False":
            if "key" in data.get("Error", "").lower():
                raise OnlineError(data["Error"])
            return []
        return [Match("OMDb", r["imdbID"], r.get("Title", ""), (r.get("Year") or "")[:4],
                      "tv" if r.get("Type") == "series" else r.get("Type", ""))
                for r in data.get("Search", []) if r.get("Type") in ("series", "movie")]
    raise OnlineError(f"unknown provider {provider}")


def episodes(match: Match, key: str) -> list[Episode]:
    """Every regular (non-special) episode of a TV match, in season order."""
    if match.kind != "tv":
        return []
    out: list[Episode] = []
    if match.provider == "TVmaze":
        for e in _get_json(f"https://api.tvmaze.com/shows/{match.id}/episodes"):
            if e.get("season") and e.get("number"):
                out.append(Episode(e["season"], e["number"], e.get("name") or ""))
    elif match.provider == "TMDb":
        show = _tmdb(key, f"/tv/{match.id}")
        for s in sorted(show.get("seasons", []), key=lambda s: s.get("season_number", 0)):
            if s.get("season_number", 0) < 1:
                continue
            season = _tmdb(key, f"/tv/{match.id}/season/{s['season_number']}")
            out += [Episode(s["season_number"], e["episode_number"], e.get("name") or "")
                    for e in season.get("episodes", [])]
    elif match.provider == "OMDb":
        k = urllib.parse.quote(key)
        show = _get_json(f"https://www.omdbapi.com/?apikey={k}&i={match.id}")
        seasons = int(show.get("totalSeasons") or 0) if str(show.get("totalSeasons", "")).isdigit() else 0
        for n in range(1, seasons + 1):
            data = _get_json(f"https://www.omdbapi.com/?apikey={k}&i={match.id}&Season={n}")
            out += [Episode(n, int(e["Episode"]), e.get("Title") or "")
                    for e in data.get("Episodes", []) if str(e.get("Episode", "")).isdigit()]
    out.sort(key=lambda e: (e.season, e.number))
    return out


def _tmdb(key: str, path: str) -> dict:
    url = f"https://api.themoviedb.org/3{path}"
    if key.startswith("eyJ"):  # a v4 read access token
        return _get_json(url, {"Authorization": f"Bearer {key}", "accept": "application/json"})
    sep = "&" if "?" in url else "?"
    return _get_json(f"{url}{sep}api_key={urllib.parse.quote(key)}")


# ---------------------------------------------------------------------------
# TheDiscDB
# ---------------------------------------------------------------------------

DISCDB_URL = "https://thediscdb.com/graphql"


def content_hash(sizes: dict[str, int]) -> str:
    """TheDiscDB's lookup key: MD5 of the media files' sizes, ordered by file name.

    `sizes` maps file names (VIDEO_TS/*.VOB/IFO/BUP on a DVD, BDMV/STREAM/*.m2ts on a
    Blu-ray) to their sizes. See TheDiscDb/optical-disc-manifest, specification §4.1.
    """
    names = sorted(sizes, key=lambda n: n.encode("utf-8"))
    return hashlib.md5(b"".join(struct.pack("<q", sizes[n]) for n in names)).hexdigest().upper()


def disc_files(root: Path) -> tuple[str, dict[str, int]]:
    """("dvd" | "blu-ray", {file name: size}) for the files TheDiscDB hashes."""
    video, stream = root / "VIDEO_TS", root / "BDMV" / "STREAM"
    if video.is_dir():
        return "dvd", {p.name: p.stat().st_size for p in video.iterdir()
                       if p.suffix.upper() in (".VOB", ".IFO", ".BUP")}
    if stream.is_dir():
        return "blu-ray", {p.name: p.stat().st_size for p in stream.iterdir() if p.suffix.lower() == ".m2ts"}
    return "", {}


@dataclass
class DiscTitle:
    item_type: str      # Episode, Extra, MainMovie, Trailer, DeletedScene...
    season: str
    episode: str
    description: str
    duration: str


@dataclass
class DiscMatch:
    title: str
    year: str
    kind: str                       # "tv" or "movie"
    release: str
    disc: str
    titles: dict[str, DiscTitle] = field(default_factory=dict)   # by source: DVD title "01" or "00800.mpls"


_DISC_QUERY = """query($h: String!) {
  mediaItems(where: {releases: {some: {discs: {some: {disc: {contentHash: {eq: $h}}}}}}}) {
    nodes { title year type
      releases { title discs { disc { name contentHash
        titles { sourceFile duration itemType season episode description } } } } }
  }
}"""


def discdb_lookup(hash_: str) -> DiscMatch | None:
    data = _get_json(DISCDB_URL, {"Content-Type": "application/json"},
                     json.dumps({"query": _DISC_QUERY, "variables": {"h": hash_}}).encode())
    if data.get("errors"):
        raise OnlineError(data["errors"][0].get("message", "query failed"))
    for item in data["data"]["mediaItems"]["nodes"]:
        for release in item.get("releases") or []:
            for d in release.get("discs") or []:
                disc = d.get("disc") or {}
                if disc.get("contentHash") != hash_:
                    continue
                titles = {t["sourceFile"]: DiscTitle(t.get("itemType") or "", t.get("season") or "",
                                                     t.get("episode") or "", t.get("description") or "",
                                                     t.get("duration") or "")
                          for t in disc.get("titles") or [] if t.get("sourceFile") and t.get("itemType")}
                kind = "tv" if (item.get("type") or "").lower() == "series" else "movie"
                return DiscMatch(item.get("title") or "", str(item.get("year") or ""), kind,
                                 release.get("title") or "", disc.get("name") or "", titles)
    return None


def identify_disc(root: Path) -> tuple[DiscMatch | None, str]:
    """Look the disc up on TheDiscDB. Returns (match or None, a line for the log)."""
    fmt, sizes = disc_files(root)
    if not sizes:
        return None, "TheDiscDB: no DVD or Blu-ray video files found"
    h = content_hash(sizes)
    try:
        match = discdb_lookup(h)
    except OnlineError as e:
        return None, f"TheDiscDB: lookup failed: {e}"
    if match is None:
        return None, f"TheDiscDB: this disc isn't in the database yet (hash {h})"
    labelled = sum(1 for t in match.titles.values() if t.item_type)
    return match, (f"TheDiscDB: {match.title} ({match.year}), {match.release}"
                   f"{f' {match.disc}' if match.disc else ''}: {labelled} titles labelled")
