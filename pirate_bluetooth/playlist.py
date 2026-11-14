"""Playlist check for the music page: which songs of an Apple Music playlist are already on the Pi.

* Reads a shared Apple Music playlist from its public web page: only the song list (title, artist,
  album, length). Apple has no official way to read playlists without a paid developer account, so this
  depends on how the page is built; if Apple changes it, the check says so instead of guessing.
* Matches each song against the music on the Pi: Mopidy's library (the tags inside the files) when it's
  on, and the file and folder names always. Clear matches are ticked off. When it isn't clear (another
  version, a remix or VIP, a different spelling, a different length), the closest songs on the Pi are
  offered to pick from.
* For songs that aren't on the Pi it gives links: the song on the iTunes Store (bought songs are normal,
  unprotected files you can drop on the music page) and searches on Bandcamp (sells most music as WAV or
  FLAC) and SoundCloud (some artists offer free downloads). Nothing is downloaded from those sites.
* Saves the matched songs, in the playlist's order, as a playlist in Mopidy (the menu's Playlists, Iris),
  and remembers your picks for the next check.

Standard library only.
"""

import difflib
import heapq
import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("pirate_upload")

APPLE_BASE = os.environ.get("PIRATE_APPLE_BASE", "https://music.apple.com")  # env: tests only
ITUNES_LOOKUP = os.environ.get("PIRATE_ITUNES_LOOKUP", "https://itunes.apple.com/lookup")  # env: tests only
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff",
              ".wma", ".ape", ".wv", ".mka", ".alac"}
MAX_PAGE = 12 * 2**20
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) PirateAudio/1.3 (playlist check)"
VERSION_WORDS = {"remix", "vip", "live", "edit", "extended", "acoustic", "instrumental", "mix", "demo",
                 "bootleg", "flip", "cover", "slowed", "sped", "nightcore", "rework", "dub", "reprise",
                 "unplugged", "orchestral", "remixed", "version", "clean", "explicit"}
NEUTRAL_WORDS = {"remaster", "remastered", "original", "mono", "stereo", "single", "album", "radio"}
SURE_TITLE = 0.92
SURE_ARTIST = 0.8
LENGTH_SLACK_S = 20  # same name but this much longer/shorter: probably another version
MIN_TITLE = 0.55  # titles less alike than this aren't offered at all


class PlaylistError(Exception):
    pass


# ==== reading the Apple Music page ==============================================================
def parse_link(text):
    """(country, playlist id) from a shared Apple Music playlist link."""
    text = (text or "").strip()
    m = re.search(r"https?://[^\s]+", text)  # the Share sheet sometimes adds words around the link
    url = urllib.parse.urlparse(m.group(0) if m else text)
    host = (url.hostname or "").lower()
    if not (host == "apple.com" or host.endswith(".apple.com")):
        raise PlaylistError("That isn't an Apple Music link. In the Music app, open the playlist, tap ⋯ → "
                            "Share Playlist → Copy, and paste it here.")
    parts = [p for p in url.path.split("/") if p]
    if "library" in parts:
        raise PlaylistError("That link only works inside your own library. In the Music app, open the playlist, "
                            "tap ⋯ → Share Playlist → Copy, and paste that link instead.")
    ids = [p for p in parts if re.fullmatch(r"pl\.(u-)?[A-Za-z0-9]+", p)]
    if "playlist" not in parts or not ids:
        raise PlaylistError("That's an Apple Music link, but not to a playlist. Share the playlist itself "
                            "(⋯ → Share Playlist → Copy).")
    country = parts[0].lower() if re.fullmatch(r"[A-Za-z]{2}", parts[0]) else "us"
    return country, ids[-1]


def fetch_page(country, playlist_id, timeout=20):
    url = f"{APPLE_BASE}/{country}/playlist/{playlist_id}"
    opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_PAGE + 1)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise PlaylistError("Apple Music says that playlist doesn't exist (or it isn't shared publicly).")
        raise PlaylistError(f"Apple Music answered with an error ({err.code}). Try again in a moment.")
    except OSError as err:
        raise PlaylistError(f"Couldn't reach Apple Music - is the Pi online? ({getattr(err, 'reason', err)})")
    if len(data) > MAX_PAGE:
        raise PlaylistError("Apple's page for that playlist is unexpectedly big.")
    return data.decode("utf-8", "replace")


def _scripts(page, attr_pattern):
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", page, re.S | re.I):
        if re.search(attr_pattern, m.group(1), re.I):
            yield m.group(2)


def _walk(node, depth=0):
    """Every dict inside a JSON value."""
    if depth > 40:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _store_id(item):
    for d in _walk(item):
        for k in ("storeAdamID", "adamId", "adamID"):
            if str(d.get(k, "")).isdigit():
                return str(d[k])
    for d in _walk(item):
        url = d.get("url") if isinstance(d.get("url"), str) else ""
        m = re.search(r"/song/(?:[^/?#]+/)?(\d{5,})", url) or re.search(r"[?&]i=(\d{5,})", url)
        if m:
            return m.group(1)
    return None


def _track_from_item(item):
    title = _text(item.get("title"))
    artist = _text(item.get("artistName"))
    if not artist and isinstance(item.get("subtitleLinks"), list):
        artist = ", ".join(_text(link.get("title")) for link in item["subtitleLinks"] if isinstance(link, dict))
    album = ""
    if isinstance(item.get("tertiaryLinks"), list) and item["tertiaryLinks"]:
        first = item["tertiaryLinks"][0]
        album = _text(first.get("title")) if isinstance(first, dict) else ""
    album = album or _text(item.get("albumName"))
    duration = item.get("duration")
    return {"title": title, "artist": artist, "album": album,
            "duration": int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
            "id": _store_id(item)}


def _iso_duration_ms(text):
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0 for g in m.groups())
    return int((h * 3600 + mi * 60 + s) * 1000)


def parse_page(page):
    """{"name", "tracks": [...], "reported": number of songs Apple says the playlist has (or None),
    "artwork": the playlist's picture (URL) or ""}."""
    name, tracks, reported, artwork = "", [], None, ""
    for raw in _scripts(page, r'id=["\']serialized-server-data["\']'):
        try:
            data = json.loads(html.unescape(raw) if raw.lstrip().startswith("&") else raw)
        except ValueError:
            continue
        best = []
        for d in _walk(data):
            items = d.get("items")
            if not isinstance(items, list):
                continue
            songs = [it for it in items if isinstance(it, dict) and _text(it.get("title"))
                     and (_text(it.get("artistName")) or it.get("subtitleLinks"))
                     and (it.get("duration") is not None or "track" in str(d.get("id", ""))
                          or "track" in str(d.get("itemKind", "")).lower()
                          or (isinstance(it.get("contentDescriptor"), dict)
                              and it["contentDescriptor"].get("kind") == "song"))]
            if len(songs) > len(best):
                best = songs
        tracks = [_track_from_item(it) for it in best]
        for d in _walk(data):
            if "header" in str(d.get("id", "")).lower() and isinstance(d.get("items"), list) and d["items"]:
                head = d["items"][0]
                if isinstance(head, dict):
                    name = name or _text(head.get("title"))
                    artwork = artwork or _artwork_url(head)
                    for k in ("trackCount", "songCount", "numberOfTracks"):
                        if isinstance(head.get(k), int):
                            reported = head[k]
        if tracks:
            break
    for raw in _scripts(page, r'(schema:music-playlist|application/ld\+json)'):
        try:
            ld = json.loads(raw)
        except ValueError:
            continue
        for d in _walk(ld):
            if d.get("@type") != "MusicPlaylist":
                continue
            name = name or _text(d.get("name"))
            if isinstance(d.get("numTracks"), int):
                reported = d["numTracks"]
            if not tracks and isinstance(d.get("track"), list):  # titles only: matching by title
                for t in d["track"]:
                    if isinstance(t, dict) and _text(t.get("name")):
                        by = t.get("byArtist")
                        artist = _text(by.get("name")) if isinstance(by, dict) else ""
                        tracks.append({"title": _text(t["name"]), "artist": artist, "album": "",
                                       "duration": _iso_duration_ms(t.get("duration")), "id": _store_id(t)})
    if not name:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', page, re.I)
        name = html.unescape(m.group(1)).strip() if m else ""
        name = re.sub(r"\s+(by .*)?on Apple Music$", "", name).strip()
    if not artwork:
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page, re.I)
        if m:  # the sharing picture (1200x630): ask Apple's image server for a square one instead
            artwork = re.sub(r"/\d+x\d+[a-z]*(-\d+)?\.(jpg|jpeg|png|webp)$", "/600x600cc.jpg", html.unescape(m.group(1)))
    if not tracks:
        raise PlaylistError("Couldn't find the songs on Apple's page for that playlist. Make sure it's shared "
                            "publicly; if it is, Apple may have changed their page and this check needs an update.")
    return {"name": name or "Apple Music playlist", "tracks": tracks, "reported": reported,
            "artwork": artwork if _image_host_ok(artwork) else ""}


def _artwork_url(item):
    """A square picture URL from Apple's artwork template ("…/{w}x{h}{c}.{f}")."""
    for d in _walk(item.get("artwork") or {}):
        url = d.get("url") if isinstance(d.get("url"), str) else d.get("template") if isinstance(d.get("template"), str) else ""
        if "{w}" in url:
            return url.replace("{w}", "600").replace("{h}", "600").replace("{c}", "cc").replace("{f}", "jpg")
    return ""


def _image_host_ok(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return url.startswith("https://") and (host.endswith(".mzstatic.com") or host.endswith(".apple.com")) \
        or url.startswith(APPLE_BASE + "/")


# ==== comparing names ===========================================================================
def _fold(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return text.replace("&", " and ").replace("$", "s")


def _words(text):
    return re.findall(r"[0-9a-z]+", _fold(text))


def split_title(title, loose=False):
    """("base title", version words) - "Song (Example VIP) [feat. X]" -> ("song", {"vip"}).

    loose: for names made from file names, where "Song VIP" or "song remix" has no brackets: version words
    at the end count too."""
    text = _fold(title)
    versions = set()
    extras = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", text)
    text = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", text)
    m = re.search(r"\s+-\s+(.*)$", text)
    if m and set(_words(m.group(1))) & (VERSION_WORDS | NEUTRAL_WORDS):
        extras.append(m.group(1))
        text = text[:m.start()]
    text = re.split(r"\s(?:feat\.?|ft\.?|featuring)\s", f" {text} ")[0]
    for extra in extras:
        words = set(_words(extra))
        if words & {"feat", "ft", "featuring", "with"} and not words & VERSION_WORDS:
            continue
        versions |= words & VERSION_WORDS
    words = _words(text)
    if loose:
        while len(words) > 1 and words[-1] in VERSION_WORDS:
            versions.add(words.pop())
    if "sped" in versions:
        versions.add("slowed")  # "sped up" / "slowed" both count as an edit
    return " ".join(words), versions


def artist_names(text):
    parts = re.split(r"\s*(?:,|&|\band\b|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|/|;|\+)\s*", _fold(text))
    return [" ".join(_words(p)) for p in parts if _words(p)]


def _ratio(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def artist_score(wanted, have):
    """wanted/have: artist text, or lists from artist_names()."""
    a = wanted if isinstance(wanted, list) else artist_names(wanted)
    b = have if isinstance(have, list) else artist_names(have)
    if not a or not b:
        return None
    best = 0.0
    for x in a:
        for y in b:
            best = max(best, 1.0 if (x == y or (len(x) > 3 and len(y) > 3 and (x in y or y in x))) else _ratio(x, y))
    return best


# ==== the music on the Pi =======================================================================
def _guess_from_path(rel):
    """Title, artist and album from a file's place in the Music folder (for files without tags)."""
    parts = rel.split("/")
    stem = os.path.splitext(parts[-1])[0]
    stem = re.sub(r"^\s*(?:\d{1,3}|[A-Da-d]\d{1,2})(?:\s*[-._)]\s*|\s+)", "", stem).strip() or stem
    folder_artist = parts[-3] if len(parts) >= 3 else ""
    album = parts[-2] if len(parts) >= 2 else ""
    artist, title = folder_artist, stem
    m = re.match(r"(.+?)\s+[-–]\s+(.+)", stem)
    if m:
        artist, title = m.group(1), m.group(2)
    return title, artist, album


class LibraryIndex:
    """Every song in the Music folder, with Mopidy's tags where Mopidy has read them."""

    def __init__(self, music_dir, rpc=None):
        self.root = Path(os.path.expanduser(music_dir))
        self.rpc = rpc  # rpc(method, **params) for Mopidy, or None
        self.source = "files"
        self._lock = threading.Lock()
        self._cache = (0.0, None)

    def invalidate(self):
        self._cache = (0.0, None)

    def entries(self):
        with self._lock:
            at, cached = self._cache
            if cached is not None and time.monotonic() - at < 60:
                return cached
            built = self._build()
            self._cache = (time.monotonic(), built)
            return built

    def _build(self):
        found = []
        root = os.path.realpath(self.root)
        if os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for f in sorted(filenames):
                    if f.startswith(".") or os.path.splitext(f)[1].lower() not in AUDIO_EXTS:
                        continue
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    title, artist, album = _guess_from_path(rel)
                    found.append({"rel": rel, "path": full, "title": title, "artist": artist, "album": album,
                                  "length": None, "tagged": False,
                                  "local_uri": "local:track:" + urllib.parse.quote(rel),
                                  "uri": "file://" + urllib.parse.quote(full)})
        self.source = "files"
        if self.rpc and found:
            try:
                by_uri = {e["local_uri"]: e for e in found}
                uris = list(by_uri)
                tagged = 0
                for i in range(0, len(uris), 100):
                    result = self.rpc("core.library.lookup", uris=uris[i:i + 100]) or {}
                    for uri, tracks in result.items():
                        if not tracks or uri not in by_uri:
                            continue
                        t, e = tracks[0], by_uri[uri]
                        names = ", ".join(a.get("name", "") for a in t.get("artists") or [] if a.get("name"))
                        if t.get("name"):
                            e["title"], e["tagged"] = t["name"], True
                        e["artist"] = names or e["artist"]
                        e["album"] = (t.get("album") or {}).get("name") or e["album"]
                        e["length"] = t.get("length")
                        e["uri"] = uri  # Mopidy knows it: play it from its library
                        tagged += 1
                self.source = "mopidy" if tagged else "files"
            except Exception as err:  # Mopidy busy, starting, or its library is off: names only
                log.info("Playlist check: Mopidy's library isn't available (%s); matching by file names", err)
        for e in found:
            _prepare(e)
        return found


STOP_WORDS = {"the", "a", "an", "of", "and", "to", "in", "on", "my", "me", "you", "i", "it", "is", "for", "your"}


def _prepare(e):
    """Work out an entry's comparable names once (matching a big playlist against a big library on a Pi
    Zero has to be quick)."""
    titles = [e["title"]]
    if not e["tagged"]:
        titles.append(os.path.splitext(e["rel"].split("/")[-1])[0])  # the whole file name too
    e["_t"] = [split_title(t, loose=not e["tagged"]) for t in titles]
    e["_an"] = artist_names(e["artist"])
    e["_name"] = " ".join(_words(e["rel"]))
    e["_w"] = {w for base, _v in e["_t"] for w in base.split()} | set(_words(e["artist"]))


def _score(want, entry, w_base, w_ver, w_artists):
    """How well a file on the Pi fits a playlist song: (score 0-1, sure?, note)."""
    t_score, e_ver = 0.0, set()
    for base, ver in entry["_t"]:
        r = _ratio(w_base, base)
        if r > t_score or (r == t_score and ver == w_ver):
            t_score, e_ver = r, ver
    a_score = artist_score(w_artists, entry["_an"])
    if a_score is None and w_artists and not entry["tagged"]:
        # no artist known for the file: the artist may be in the file name ("Artist - Title", "title artist")
        a_score = 0.9 if any(a in entry["_name"] for a in w_artists) else None
    score = t_score * 0.7 + (a_score if a_score is not None else 0.5) * 0.3
    notes = []
    if w_ver != e_ver:
        score -= 0.12
        notes.append(f"{'/'.join(sorted(e_ver)) or 'original'} version"
                     + (f" (playlist: {'/'.join(sorted(w_ver))})" if w_ver else ""))
    sure = t_score >= SURE_TITLE and (a_score or 0) >= SURE_ARTIST and w_ver == e_ver
    if want.get("duration") and entry.get("length"):
        diff = (entry["length"] - want["duration"]) / 1000
        if abs(diff) > LENGTH_SLACK_S:
            sure = False
            score -= 0.05
            notes.append(f"{abs(diff):.0f} s {'longer' if diff > 0 else 'shorter'}")
    if a_score is None:
        notes.append("artist unknown")
        sure = False
    elif a_score < SURE_ARTIST:
        notes.append("different artist")
    if t_score < MIN_TITLE:
        score = 0.0  # same artist, different song: not worth offering
    return max(0.0, score), sure, ", ".join(notes)


def match(tracks, entries, picks=None, limit=4):
    """Status per playlist song: "on_pi" (with match), "choose" (with candidates) or "missing"."""
    picks = picks or {}
    by_uri = {e["uri"]: e for e in entries}
    by_uri.update({e["local_uri"]: e for e in entries})
    postings = {}
    for i, e in enumerate(entries):
        if "_t" not in e:
            _prepare(e)
        for w in e["_w"]:
            postings.setdefault(w, []).append(i)
    out = []
    for n, want in enumerate(tracks):
        key = track_key(want)
        w_base, w_ver = split_title(want["title"])
        w_artists = artist_names(want["artist"])
        # Only files that share words with the song's title or artist are compared closely, and only the
        # 60 that share the most (a big playlist against a big library has to be quick on a Pi Zero).
        t_words = {w for w in w_base.split() if w not in STOP_WORDS} or set(w_base.split())
        a_words = {w for a in w_artists for w in a.split() if w not in STOP_WORDS}
        shared = {}
        for w in t_words:
            for i in postings.get(w, ()):
                shared[i] = shared.get(i, 0) + 2
        for w in a_words:
            for i in postings.get(w, ()):
                shared[i] = shared.get(i, 0) + 1
        near = heapq.nlargest(60, shared, key=lambda i: (shared[i], -i))
        scored = []
        for i in near:
            s, sure, note = _score(want, entries[i], w_base, w_ver, w_artists)
            if s >= 0.45:
                scored.append((s, sure, note, entries[i]))
        scored.sort(key=lambda x: (-x[1], -x[0], x[3]["rel"]))
        row = {"n": n + 1, "key": key, **{k: want[k] for k in ("title", "artist", "album", "duration", "id")},
               "candidates": [_cand(e, s, note) for s, _sure, note, e in scored[:limit]]}
        pick = picks.get(key)
        if pick == "none":
            row.update(status="missing", picked=True)
        elif pick and pick in by_uri:
            e = by_uri[pick]
            row.update(status="on_pi", picked=True, match=_cand(e, 1.0, "your pick"))
            if not any(c["uri"] == e["uri"] for c in row["candidates"]):
                row["candidates"].insert(0, row["match"])
        elif scored and scored[0][1] and not (len(scored) > 1 and scored[1][1] and scored[1][0] >= scored[0][0] - 0.02
                                                 and scored[1][3]["title"] != scored[0][3]["title"]):
            row.update(status="on_pi", match=_cand(scored[0][3], scored[0][0], scored[0][2]))
        elif scored:
            row.update(status="choose")
        else:
            row.update(status="missing")
        out.append(row)
    return out


def _cand(e, score, note):
    return {"uri": e["uri"], "path": e["rel"], "title": e["title"], "artist": e["artist"], "album": e["album"],
            "length": e["length"], "score": round(score, 2), "note": note}


def track_key(t):
    if t.get("id"):
        return f"am:{t['id']}"
    base, ver = split_title(t["title"])
    return "t:" + "|".join([" ".join(artist_names(t["artist"])), base, "/".join(sorted(ver))])


# ==== where to get missing songs ====================================================================
def store_links(rows, country="us", timeout=10):
    """iTunes Store page and price for songs that aren't (clearly) on the Pi - one request per 150 songs."""
    ids = [r["id"] for r in rows if r.get("id") and r["status"] != "on_pi"]
    found = {}
    opener = urllib.request.build_opener()
    for i in range(0, len(ids), 150):
        query = urllib.parse.urlencode({"id": ",".join(ids[i:i + 150]), "country": country, "entity": "song"})
        try:
            with opener.open(f"{ITUNES_LOOKUP}?{query}", timeout=timeout) as resp:
                results = json.loads(resp.read(4 * 2**20)).get("results", [])
        except (OSError, ValueError) as err:
            log.info("iTunes Store lookup didn't work: %s", err)
            break
        for r in results:
            if r.get("wrapperType") == "track" and r.get("trackViewUrl"):
                url = r["trackViewUrl"]
                url += ("&" if "?" in url else "?") + "app=itunes"
                price = r.get("trackPrice")
                found[str(r.get("trackId"))] = {
                    "url": url,
                    "price": f"{price:.2f} {r.get('currency', '')}".strip() if isinstance(price, (int, float)) and price > 0 else "",
                }
    for r in rows:
        q = urllib.parse.quote(f"{r['artist']} {split_title(r['title'])[0]}".strip())
        r["links"] = {"bandcamp": f"https://bandcamp.com/search?q={q}&item_type=t",
                      "soundcloud": f"https://soundcloud.com/search/sounds?q={q}"}
        if r.get("id") in found:
            r["links"]["itunes"] = found[r["id"]]["url"]
            r["links"]["price"] = found[r["id"]]["price"]
    return rows


# ==== remembering picks, saving the playlist =====================================================
class Picks:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, changes):
        with self._lock:
            data = self.load()
            for k, v in changes.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)


def clean_name(name):
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", name or "").strip(" .")
    return re.sub(r"\s+", " ", name)[:80] or "Apple Music playlist"


def save_playlist(name, songs, rpc=None, music_dir="~/Music"):
    """Save songs [{uri, title, artist}] as a playlist. Returns (where, how many, name, playlist URI)."""
    name = clean_name(name)
    tracks = [{"__model__": "Track", "uri": s["uri"], "name": s.get("title") or "",
               **({"artists": [{"__model__": "Artist", "name": s["artist"]}]} if s.get("artist") else {})}
              for s in songs if s.get("uri")]
    if rpc:
        try:
            existing = [p for p in (rpc("core.playlists.as_list") or [])
                        if p.get("name") == name and str(p.get("uri", "")).startswith("m3u:")]
            playlist = (rpc("core.playlists.lookup", uri=existing[0]["uri"]) if existing
                        else rpc("core.playlists.create", name=name, uri_scheme="m3u"))
            if playlist:
                playlist["tracks"] = tracks
                saved = rpc("core.playlists.save", playlist=playlist)
                if saved:
                    return "mopidy", len(tracks), name, saved.get("uri") or playlist.get("uri", "")
        except Exception as err:
            log.info("Saving the playlist through Mopidy didn't work (%s); writing an .m3u8 file", err)
    folder = Path(os.path.expanduser(music_dir)) / "Playlists"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for t in tracks:
        lines.append(f"#EXTINF:-1,{t['name']}")
        uri = t["uri"]
        lines.append(urllib.parse.unquote(uri[len("file://"):]) if uri.startswith("file://") else uri)
    target = folder / f"{name}.m3u8"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target), len(tracks), name, "m3u:" + urllib.parse.quote(target.name)


def mopidy_rpc(url):
    """rpc(method, **params) talking to Mopidy's JSON-RPC (on this Pi: no proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(method, timeout=30, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(err.get("message", err) if isinstance(err, dict) else err)
        return reply.get("result")
    return rpc


def check(link, index, picks, timeout=20):
    """The whole check for one link: what the music page shows."""
    country, playlist_id = parse_link(link)
    page = parse_page(fetch_page(country, playlist_id, timeout=timeout))
    entries = index.entries()
    rows = match(page["tracks"], entries, picks.load())
    store_links(rows, country)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("on_pi", "choose", "missing")}
    note = ""
    if page["reported"] and page["reported"] > len(rows):
        note = (f"Apple's page only lists the first {len(rows)} of the playlist's {page['reported']} songs, "
                "so the rest aren't checked.")
    return {"name": page["name"], "country": country, "id": playlist_id, "tracks": rows, "counts": counts,
            "library": getattr(index, "source", "files"), "songs_on_pi": len(entries), "note": note,
            "artwork": page.get("artwork", ""), "page_tracks": page["tracks"]}
