#!/usr/bin/env python3
"""Copy a Plex music library for the car: lossless files become Opus, lossy ones are copied as they are.

    plex_music.py --server http://plex.local:32400 --section 3 --token-file plex.token --out ~/CarMusic

Keeps Plex's folder layout (Artist/Album/track) and puts each album's Plex cover in its folder as cover.jpg
(conversion drops embedded art). Writes library.json with Plex's clean artist/album/title/track/year for each
file, which the head unit prefers over the folder names. Resumable: files already in --out are skipped, so run it
again to pick up new music. --art-only fetches just the covers and the index. Needs ffmpeg with libopus.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402  (PLEX_* in nomad.conf)

LOSSY = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}  # copied unchanged (m4a may be ALAC: checked by codec)
LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "pcm", "wav"}


def plex(server, token, path, **params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{server}{path}?{q}", headers={"X-Plex-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["MediaContainer"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=config.get("PLEX_SERVER"), required=not config.get("PLEX_SERVER"))
    ap.add_argument("--section", default=config.get("PLEX_SECTION"), required=not config.get("PLEX_SECTION"),
                    help="library section id of the music library")
    ap.add_argument("--token-file", default=config.get("PLEX_TOKEN_FILE"), required=not config.get("PLEX_TOKEN_FILE"))
    ap.add_argument("--out", default=config.get("MUSIC_OUT"), required=not config.get("MUSIC_OUT"))
    ap.add_argument("--bitrate", default="160k")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--art-only", action="store_true", help="only fetch album covers")
    args = ap.parse_args()
    token = pathlib.Path(args.token_file).read_text().strip()
    out = pathlib.Path(args.out).expanduser()
    tmp = out / ".partial"
    tmp.mkdir(parents=True, exist_ok=True)

    roots = [loc["path"] for d in plex(args.server, token, "/library/sections")["Directory"]
             if d["key"] == args.section for loc in d.get("Location", [])]
    tracks = plex(args.server, token, f"/library/sections/{args.section}/all", type=10).get("Metadata", [])
    jobs, covers, index = [], {}, {}
    for t in tracks:
        for m in t.get("Media", []):
            for p in m.get("Part", []):
                src = p["file"]
                root = next((r for r in roots if src.startswith(r.rstrip("/") + "/")), None)
                rel = pathlib.PurePosixPath(src[len(root.rstrip("/")) + 1:] if root else pathlib.PurePosixPath(src).name)
                convert = (m.get("audioCodec") or "").lower() in LOSSLESS_CODECS or rel.suffix.lower() not in LOSSY
                dest = out / (rel.with_suffix(".opus") if convert else rel)
                jobs.append((p["key"], p.get("size", 0), dest, convert))
                index[dest.relative_to(out).as_posix()] = {
                    "artist": t.get("originalTitle") or t.get("grandparentTitle") or "",
                    "album_artist": t.get("grandparentTitle") or "", "album": t.get("parentTitle") or "",
                    "title": t.get("title") or "", "track": t.get("index") or 0, "disc": t.get("parentIndex") or 1,
                    "year": t.get("parentYear") or t.get("year") or 0}
                if t.get("parentThumb"):
                    covers.setdefault(dest.parent / "cover.jpg", t["parentThumb"])
    index_tmp = out / ".library.json.tmp"
    index_tmp.write_text(json.dumps({"source": "plex", "tracks": index}, ensure_ascii=False))
    index_tmp.replace(out / "library.json")
    print(f"index: {len(index)} tracks in library.json", flush=True)
    fetch_covers(args.server, token, covers)
    if args.art_only:
        return 0
    todo = [j for j in jobs if not j[2].exists()]
    total_bytes = sum(j[1] for j in todo)
    print(f"{len(jobs)} tracks, {len(jobs) - len(todo)} already done, {len(todo)} to go ({total_bytes / 1e9:.1f} GB to read)",
          flush=True)

    lock = threading.Lock()
    stats = {"done": 0, "fail": 0, "read": 0, "written": 0}
    t0 = time.time()
    failures = open(out / "failures.txt", "a")

    up = threading.Event()
    up.set()

    def wait_for_server():
        """The first worker to see the server gone polls until it is back; the rest wait on it."""
        with lock:
            if not up.is_set():
                return
            up.clear()
        print(f"  server unreachable at {time.strftime('%H:%M:%S')}, waiting", flush=True)
        while True:
            try:
                plex(args.server, token, "/identity")
                break
            except OSError:
                time.sleep(30)
        print(f"  server back at {time.strftime('%H:%M:%S')}", flush=True)
        up.set()

    def work(job, attempt=0):
        key, size, dest, convert = job
        up.wait()
        part = tmp / f"{abs(hash(key))}{dest.suffix if not convert else '.src'}"
        try:
            req = urllib.request.Request(f"{args.server}{key}?download=1", headers={"X-Plex-Token": token})
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            dest.parent.mkdir(parents=True, exist_ok=True)
            final_tmp = dest.with_name(dest.name + ".tmp")
            if convert:
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(part), "-map", "0:a:0",
                                "-map_metadata", "0", "-c:a", "libopus", "-b:a", args.bitrate, "-vbr", "on",
                                "-f", "ogg", str(final_tmp)], check=True, timeout=600)
                part.unlink()
            else:
                part.replace(final_tmp)
            final_tmp.replace(dest)
            with lock:
                stats["done"] += 1
                stats["read"] += size
                stats["written"] += dest.stat().st_size
        except (OSError, subprocess.SubprocessError) as e:
            part.unlink(missing_ok=True)
            if isinstance(e, OSError) and not isinstance(e, urllib.error.HTTPError) and attempt < 3:
                try:
                    plex(args.server, token, "/identity")
                except OSError:
                    wait_for_server()
                    up.wait()
                return work(job, attempt + 1)
            with lock:
                stats["fail"] += 1
                failures.write(f"{key}\t{dest}\t{e}\n")
                failures.flush()

    with ThreadPoolExecutor(args.jobs) as pool:
        for i, _ in enumerate(pool.map(work, todo), 1):
            if i % 50 == 0 or i == len(todo):
                with lock:
                    el = time.time() - t0
                    rate = stats["read"] / el
                    eta = (total_bytes - stats["read"]) / rate / 3600 if rate else 0
                    print(f"  {i}/{len(todo)}  read {stats['read'] / 1e9:.1f} GB at {rate / 1e6:.1f} MB/s, "
                          f"wrote {stats['written'] / 1e9:.1f} GB, {stats['fail']} failed, ETA {eta:.1f} h", flush=True)
    print(f"done: {stats}", flush=True)
    return 1 if stats["fail"] else 0


def fetch_covers(server, token, covers):
    """Album covers from Plex, scaled to 600 px so they stay small."""
    todo = {dest: thumb for dest, thumb in covers.items() if not dest.exists()}
    got = 0
    for dest, thumb in todo.items():
        q = urllib.parse.urlencode({"width": 600, "height": 600, "minSize": 1, "upscale": 0, "url": thumb})
        req = urllib.request.Request(f"{server}/photo/:/transcode?{q}", headers={"X-Plex-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            got += 1
        except OSError:
            continue
    print(f"album covers: {got} new, {len(covers) - len(todo)} already there, {len(todo) - got} failed", flush=True)


if __name__ == "__main__":
    sys.exit(main())
