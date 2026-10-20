"""The local LLM behind voice (llama-server, Gemma 4 E2B on 127.0.0.1:8090, run by llm.service).

Requests that start with the keyword (NOMAD_NAME, "truck, ..." by default) are commands: app.js tries its fixed grammar, then command()
picks an action under a JSON schema, so the answer is always parseable. Everything else is conversation: chat()
answers in a sentence or two, remembers the last few exchanges for a few minutes so follow-ups work, and gets a
context line (time, route, music) with each request. The system prompts stay in llama-server's prompt cache.
tools/bench_intents.py scores models against command().
"""
import json
import os
import re
import threading
import time
import urllib.request

import config

LLM = os.environ.get("HEADUNIT_LLM", "http://127.0.0.1:8090")
MEMORY_S = 180  # a follow-up more than this long after the last exchange starts a new conversation
MEMORY_TURNS = 6

ACTIONS = ["navigate", "cancel_navigation", "play_music", "pause", "next_track", "previous_track",
           "volume_up", "volume_down", "zoom_in", "zoom_out", "show_screen", "save_clip",
           "ask_speed", "ask_time", "ask_eta", "unknown"]

SYSTEM = """You turn a driver's spoken request into one command for a car head unit.
Actions:
- navigate: drive somewhere. query = the place, business or kind of place ("gas station", "Safeway", "pizza").
- cancel_navigation: stop or cancel the current route.
- play_music: play music. query = the song, artist or album asked for, or "" for any music.
- pause, next_track, previous_track: control the music.
- volume_up, volume_down: louder or quieter.
- zoom_in, zoom_out: the map.
- show_screen: query = one of map, media, cameras, vehicle, settings.
- save_clip: keep the current dashcam recording.
- ask_speed, ask_time, ask_eta: the driver asks for speed, the time, or when they will arrive.
- unknown: anything else, or nothing actionable.
Reply with JSON only."""

SCHEMA = {"type": "object", "properties": {"action": {"type": "string", "enum": ACTIONS},
                                           "query": {"type": "string"}},
          "required": ["action", "query"], "additionalProperties": False}

CHAT_SYSTEM = f"""You are the voice of a car's head unit, talking with the driver while they drive.
Answer the way a friendly passenger would: natural, warm, and brief. At most two short sentences, because it is
read out loud; no lists, no symbols, no emoji. Use the facts under "Right now" when they help, and never repeat
them back word for word. You can't look anything up or control the truck in this mode, so never say you did
something or will do it; if the driver wants something done, say to start with the word "{config.NAME.lower()}". If you don't
know something, say so."""

_lock = threading.Lock()
_history = []  # [user message, assistant message, ...]
_last = 0.0


def _complete(messages, max_tokens, timeout, llm, **extra):
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}, **extra}
    req = urllib.request.Request(f"{llm}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def command(text, timeout=20, llm=LLM):
    """{"action", "query", "llm_s"} for one command. Raises OSError when llama-server isn't running."""
    t = time.time()
    raw = _complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}], 60, timeout, llm,
                    response_format={"type": "json_schema", "json_schema": {"name": "command", "schema": SCHEMA}})
    try:
        got = json.loads(raw)
    except ValueError:
        got = {}
    action = got.get("action") if got.get("action") in ACTIONS else "unknown"
    return {"action": action, "query": str(got.get("query", "")).strip(), "llm_s": round(time.time() - t, 2)}


def chat(text, context="", timeout=30, llm=LLM):
    """{"reply", "llm_s"}: a spoken answer, remembering the conversation for MEMORY_S."""
    global _last
    # Context goes at the end of the system prompt (the cached part stays the same) and isn't kept in the history.
    system = f"{CHAT_SYSTEM}\n\nRight now: {context or 'unknown'}."
    user = {"role": "user", "content": text}
    with _lock:
        if time.time() - _last > MEMORY_S:
            _history.clear()
        past = list(_history)
    t = time.time()
    reply = _complete([{"role": "system", "content": system}, *past, user], 90, timeout, llm)
    reply = re.sub(r"^\s*\[[^\]]*\]\s*", "", reply).strip()  # an echoed context line
    with _lock:
        _history.extend([user, {"role": "assistant", "content": reply}])
        del _history[:-2 * MEMORY_TURNS]
        _last = time.time()
    return {"reply": reply, "llm_s": round(time.time() - t, 2)}


ASSISTANT_SYSTEM = """You are the assistant on a pickup truck's head unit, in the Assistant tab: the driver or a
passenger is typing or talking to you directly and can read your answer on screen. Be genuinely helpful and
friendly. Keep answers short unless asked for detail; plain text with simple lists is fine. You run fully offline on
the truck's computer: you can't browse the web or control the truck, and you don't know anything after your
training. The facts under "Right now" are live; use them when they help."""


def assistant_stream(messages, context="", think=True, timeout=300, llm=LLM):
    """Stream an answer to the conversation messages ([{role, content}, ...]) as (kind, text) pairs: kind is
    "think" for Gemma's reasoning (when think is on) and "text" for the answer. Raises OSError if llama-server is down."""
    msgs = [{"role": "system", "content": f"{ASSISTANT_SYSTEM}\n\nRight now: {context or 'unknown'}."}]
    msgs += [{"role": m["role"], "content": str(m["content"])[:4000]} for m in messages[-12:]
             if m.get("role") in ("user", "assistant") and m.get("content")]
    body = {"messages": msgs, "stream": True, "temperature": 0.6, "max_tokens": 1500, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": bool(think)}}
    req = urllib.request.Request(f"{llm}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                continue
            try:
                delta = json.loads(line[6:])["choices"][0].get("delta", {})
            except (ValueError, KeyError, IndexError):
                continue
            if delta.get("reasoning_content"):
                yield "think", delta["reasoning_content"]
            if delta.get("content"):
                yield "text", delta["content"]


def warm_loop():
    """Once llama-server is up, cache both system prompts before the driver speaks."""
    while True:
        try:
            command("Pause.", timeout=120)
            _complete([{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": "Hi"}], 1, 120, LLM)
            return
        except OSError:
            time.sleep(10)
