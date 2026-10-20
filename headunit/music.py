"""Music beyond artist/album/song: categories (moods, styles, genres, decades from Plex's artist and album records in
library.json), saved playlists, and mixes from a described vibe ("chill for a night drive", "like Blink-182"),
where the voice model picks tags that actually exist in the library and the tracks are chosen by how well they match.
"""
import collections
import json
import os
import pathlib
import random
import threading
import time
import urllib.request

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
PLAYLISTS = DATA / "playlists.json"
LLM = os.environ.get("HEADUNIT_LLM", "http://127.0.0.1:8090")
_lock = threading.Lock()


def track_tags(t, meta):
    """The moods, styles and genres of a track: its album's when Plex tagged the album, else its artist's. Album
    tags are the specific ones; an artist's cover a whole career (Elvis is Rockabilly *and* Gospel *and* Country)."""
    al = meta["albums"].get(f"{t['album_artist']}\t{t['album']}", {})
    ar = meta["artists"].get(t["album_artist"], {})
    return {k: set(al.get(k) or ar.get(k) or []) for k in ("mood", "style", "genre")}


def categories(tracks, meta):
    """{"mood": [[tag, songs], ...], "style": ..., "genre": ..., "decade": ...}, most albums first: ranked by albums,
    not songs, so one artist with a huge catalog doesn't fill the top of every list."""
    songs = {k: collections.Counter() for k in ("mood", "style", "genre", "decade")}
    albums = {k: collections.defaultdict(set) for k in songs}
    for t in tracks:
        key = (t["album_artist"], t["album"])
        tags = track_tags(t, meta)
        if t.get("year"):
            tags["decade"] = {f"{t['year'] // 10 * 10}s"}
        for k, v in tags.items():
            songs[k].update(v)
            for tag in v:
                albums[k][tag].add(key)
    out = {k: [[tag, songs[k][tag]] for tag in sorted(albums[k], key=lambda tag: (-len(albums[k][tag]), -songs[k][tag]))
               if songs[k][tag] >= 3] for k in songs}
    out["decade"].sort()
    return out


def tag_tracks(tracks, meta, kind, tag):
    """Indices of the tracks with a tag (kind: mood, style, genre, decade)."""
    if kind == "decade":
        d = int(tag[:4])
        return [i for i, t in enumerate(tracks) if t.get("year") and t["year"] // 10 * 10 == d]
    return [i for i, t in enumerate(tracks) if tag in track_tags(t, meta)[kind]]


# ---- vibe mixes ----
VIBE_SYSTEM = """You make a music mix for a driver from their own library. Turn the request into tags, choosing only
from the lists below (exact spellings). Pick 2-4 moods and 0-3 styles that fit the feeling. Leave artists empty
unless the request names an artist (or says "like <artist>"); leave decades empty unless it implies an era.
Name the mix in 2-4 words."""


def vibe(text, tracks, meta, n=60):
    """A mix for a described vibe: {"name", "tags", "tracks": [indices]}. Raises OSError if the model is down."""
    cats = categories(tracks, meta)
    moods = [t for t, _ in cats["mood"][:90]]
    styles = [t for t, _ in cats["style"][:90]]
    decades = [t for t, _ in cats["decade"]]
    artists = sorted({t["album_artist"] for t in tracks if t["album_artist"]})
    lst = lambda xs, n: {"type": "array", "items": {"type": "string", "enum": xs}, "maxItems": n}  # noqa: E731
    schema = {"type": "object", "required": ["name", "moods", "styles", "artists", "decades"], "additionalProperties": False,
              "properties": {"name": {"type": "string", "maxLength": 40}, "moods": lst(moods, 4), "styles": lst(styles, 3),
                             "artists": lst(artists or [""], 2), "decades": lst(decades or [""], 2)}}
    # The lists go in the system prompt, which stays the same between requests, so llama-server keeps it cached.
    system = (f"{VIBE_SYSTEM}\n\nMoods: {', '.join(moods)}\nStyles: {', '.join(styles)}\nDecades: {', '.join(decades)}\n"
              f"Artists: {', '.join(artists)}")
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": f"Request: {text}"}],
            "temperature": 0.3, "max_tokens": 200, "cache_prompt": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "mix", "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{LLM}/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        pick = json.loads(json.load(r)["choices"][0]["message"]["content"])
    # an artist counts only if the request names it (the model sometimes adds one of its own)
    said = _plain(text)
    pick["artists"] = [a for a in pick.get("artists") or [] if a and _plain(a) and _plain(a) in said]
    return {"name": pick.get("name") or text, "tags": pick, "tracks": mix(tracks, meta, pick, n)}


def mix(tracks, meta, pick, n=60):
    """Score every track against the picked tags, then draw n from the best, at most a few per artist and each song
    once, shuffled. Too few matches: try again without the era, then with styles alone."""
    out = _mix(tracks, meta, pick, n)
    if len(out) < n // 2 and pick.get("decades"):
        out = _mix(tracks, meta, {**pick, "decades": []}, n)
    if len(out) < n // 3:
        out = _mix(tracks, meta, {**pick, "decades": [], "moods": [], "styles": (pick.get("styles") or []) + _near_styles(meta, pick)}, n) or out
    return out


def _near_styles(meta, pick):
    """Styles that go with the picked moods, across the library: the fallback when the moods alone match little."""
    moods = set(pick.get("moods") or [])
    c = collections.Counter(s for al in meta["albums"].values() if moods & set(al.get("mood") or []) for s in al.get("style") or [])
    return [s for s, _ in c.most_common(4)]


def _fit_styles(meta, pick):
    """The picked styles that go with the picked moods somewhere in the library (the model sometimes adds an odd
    one: "Aussie Rock" for a chill drive)."""
    moods, styles = set(pick.get("moods") or []), list(pick.get("styles") or [])
    if not moods:
        return styles
    seen = {s for al in list(meta["albums"].values()) + list(meta["artists"].values())
            if moods & set(al.get("mood") or []) for s in al.get("style") or []}
    return [s for s in styles if s in seen] or styles


def _mix(tracks, meta, pick, n):
    moods, styles = set(pick.get("moods") or []), set(_fit_styles(meta, pick))
    decades = {int(d[:4]) for d in pick.get("decades") or [] if d[:4].isdigit()}
    named = set(pick.get("artists") or []) - {""}
    similar = {s for a in named for s in meta["artists"].get(a, {}).get("similar", [])}
    max_pop = max((t.get("pop") or 0 for t in tracks), default=0) or 1
    scored = []
    for i, t in enumerate(tracks):
        tg = track_tags(t, meta)
        mood_hits, style_hits = len(moods & tg["mood"]), len(styles & tg["style"])
        kin = t["album_artist"] in named or t["album_artist"] in similar
        if styles and not style_hits and not kin:
            continue  # a different kind of music, whatever its mood (Elvis in a pop-punk mix)
        s = 2 * mood_hits + 1.5 * style_hits
        if moods and not mood_hits and t["album_artist"] not in named:
            s -= 1
        if t["album_artist"] in named:
            s += 6
        elif t["album_artist"] in similar:
            s += 3
        if decades and t.get("year") and t["year"] // 10 * 10 in decades:
            s += 2
        elif decades:
            s -= 1
        if s <= 0:
            continue
        s += 1.5 * (t.get("pop") or 0) / max_pop + random.random()  # a little toward the hits, and a little chance
        scored.append((s, i))
    scored.sort(reverse=True)
    per_artist = collections.Counter()
    out, songs = [], set()
    # a few songs per artist, more when only a few artists fit (fifties rock and roll is mostly Elvis)
    fitting = len({tracks[i]["album_artist"] for _, i in scored}) or 1
    cap = max(4, -(-n // fitting))
    for _, i in scored[: n * 10]:
        a, song = tracks[i]["album_artist"], (tracks[i]["album_artist"].lower(), _plain(tracks[i]["title"]))
        if per_artist[a] >= (max(cap, 10) if a in named else cap) or song in songs:  # compilations repeat songs
            continue
        per_artist[a] += 1
        songs.add(song)
        out.append(i)
        if len(out) == n:
            break
    random.shuffle(out)
    return out


def _plain(title):
    """A song's title without "(Live)", "- Remastered 2009" and punctuation, to spot the same song twice."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", re.split(r" [-(\[]", title.lower())[0]).strip()


# ---- playlists: [{"id", "name", "tracks": [track ids], "updated"}] ----
def playlists():
    try:
        return json.loads(PLAYLISTS.read_text())
    except (OSError, ValueError):
        return []


def _save(lists):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLISTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(lists, ensure_ascii=False))
    tmp.replace(PLAYLISTS)
    return lists


def playlist_create(name, track_ids):
    with _lock:
        lists = playlists()
        pl = {"id": f"p{int(time.time() * 1000)}", "name": (name or "Playlist").strip()[:80], "tracks": list(track_ids)[:5000],
              "updated": time.time()}
        return _save([pl, *lists]), pl


def playlist_add(pid, track_ids):
    with _lock:
        lists = playlists()
        for pl in lists:
            if pl["id"] == pid:
                pl["tracks"] += [t for t in track_ids if t not in pl["tracks"]]
                pl["updated"] = time.time()
        return _save(lists)


def playlist_remove_track(pid, track_id):
    with _lock:
        lists = playlists()
        for pl in lists:
            if pl["id"] == pid:
                pl["tracks"] = [t for t in pl["tracks"] if t != track_id]
        return _save(lists)


def playlist_delete(pid):
    with _lock:
        return _save([pl for pl in playlists() if pl["id"] != pid])
