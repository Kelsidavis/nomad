"""News from RSS and Atom feeds: fetched every half hour when the data setting allows background downloads (they're a
few hundred KB), kept on disk so the last headlines are there without signal. The feed list lives in
DATA/news.json and is edited from the News screen; NEWS_FEEDS in nomad.conf ("Name|url, Name|url") replaces the
starting list.
"""
import email.utils
import html
import json
import os
import pathlib
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

import config
import network

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FEEDS = DATA / "news.json"
CACHE = DATA / "news-cache.json"
DEFAULT_FEEDS = [
    {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Guardian US", "url": "https://www.theguardian.com/us-news/rss"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
]
KEEP = 40  # items per feed
EVERY = 1800
_lock = threading.Lock()
_state = {"t": 0, "errors": {}}


def feeds():
    try:
        return json.loads(FEEDS.read_text())
    except (OSError, ValueError):
        pass
    conf = config.get("NEWS_FEEDS")
    if conf:
        out = []
        for part in conf.split(","):
            name, _, url = part.strip().partition("|")
            if url:
                out.append({"name": name.strip(), "url": url.strip()})
        if out:
            return out
    return list(DEFAULT_FEEDS)


def _save_feeds(lst):
    DATA.mkdir(parents=True, exist_ok=True)
    FEEDS.write_text(json.dumps(lst, ensure_ascii=False))
    return lst


def add_feed(url, name=None):
    """Add a feed after checking it parses; its name comes from the feed's own title unless given."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    if any(f["url"] == url for f in feeds()):
        raise ValueError("That feed is already in the list")
    title, items = _parse(_get(url))
    if not items:
        raise ValueError("No news items in that feed")
    with _lock:
        lst = _save_feeds(feeds() + [{"name": (name or title or url)[:40], "url": url}])
        cache = _cache()
        cache[url] = items[:KEEP]
        _save_cache(cache)
    return lst


def remove_feed(url):
    with _lock:
        lst = _save_feeds([f for f in feeds() if f["url"] != url])
        cache = _cache()
        cache.pop(url, None)
        _save_cache(cache)
    return lst


def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False))
    tmp.replace(CACHE)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "NOMAD-headunit/1.0 (RSS reader)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(4_000_000)


def _text(s):
    """Plain text from a feed's HTML summary."""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def _when(s):
    if not s:
        return 0
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()  # RSS: "Tue, 20 Oct 2026 04:12:00 GMT"
    except (TypeError, ValueError):
        pass
    try:  # Atom: ISO 8601
        from datetime import datetime
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _parse(raw):
    """(feed title, [{"title", "link", "summary", "t"}]) from RSS 2.0, RSS 1.0 or Atom."""
    root = ET.fromstring(raw)
    strip = lambda tag: tag.rsplit("}", 1)[-1]  # noqa: E731  namespaces don't matter here
    for el in root.iter():
        el.tag = strip(el.tag)
    title = (root.findtext("channel/title") or root.findtext("title") or "").strip()
    items = []
    for it in list(root.iter("item")) + list(root.iter("entry")):
        link = it.findtext("link") or ""
        if not link.strip():
            el = next((l for l in it.findall("link") if l.get("rel", "alternate") == "alternate"), None)
            link = el.get("href", "") if el is not None else ""
        summary = it.findtext("description") or it.findtext("summary") or it.findtext("content") or it.findtext("encoded") or ""
        t = _when(it.findtext("pubDate") or it.findtext("published") or it.findtext("updated") or it.findtext("date"))
        head = _text(it.findtext("title"))
        if head:
            items.append({"title": head, "link": link.strip(), "summary": _text(summary)[:600], "t": t})
    items.sort(key=lambda i: -i["t"])
    return title, items


def refresh(force=False):
    """Fetch every feed (if it's been half an hour, or force). Returns {url: error} for the ones that failed."""
    if not force and time.time() - _state["t"] < EVERY:
        return _state["errors"]
    errors = {}
    fresh = {}
    for f in feeds():
        try:
            fresh[f["url"]] = _parse(_get(f["url"]))[1][:KEEP]
        except (OSError, ValueError, ET.ParseError) as e:
            errors[f["url"]] = str(e)[:120]
    with _lock:
        cache = _cache()
        cache.update(fresh)
        _save_cache(cache)
    _state.update(t=time.time(), errors=errors)
    return errors


def headlines(feed=None, n=80):
    """{"items": [{"feed", "title", "link", "summary", "t"}] newest first, "feeds", "updated", "errors"}."""
    cache, lst = _cache(), feeds()
    names = {f["url"]: f["name"] for f in lst}
    items = [{**i, "feed": names[url]} for url, its in cache.items() if url in names and (not feed or url == feed) for i in its]
    items.sort(key=lambda i: -i["t"])
    return {"items": items[:n], "feeds": lst, "updated": _state["t"] or (CACHE.stat().st_mtime if CACHE.exists() else 0),
            "errors": _state["errors"]}


def news_loop():
    time.sleep(60)  # let boot settle
    while True:
        if network.allowed("background"):
            try:
                refresh()
            except Exception as e:  # noqa: BLE001  a bad feed must never stop the loop
                _state["errors"] = {"loop": str(e)[:120]}
        time.sleep(300)
