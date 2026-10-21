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
           "ask_speed", "ask_time", "ask_eta", "find_along_route", "set_timer", "remind", "engine_codes",
           "ask_fuel_economy", "ask_gas_price", "ask_speed_limit", "ask_location", "more_like_this", "read_news",
           "ask_aircraft", "mesh_send", "ask_oil_change", "help", "unknown"]

SYSTEM = """You turn a driver's spoken request into one command for a car head unit.
Actions:
- navigate: drive somewhere. query = the place, business or kind of place ("gas station", "Safeway", "pizza").
- cancel_navigation: stop or cancel the current route.
- play_music: play music. query = the song, artist or album asked for, or "" for any music.
- pause, next_track, previous_track: control the music.
- volume_up, volume_down: louder or quieter.
- zoom_in, zoom_out: the map.
- show_screen: only to open a screen; query = one of map, media, cameras, vehicle, settings.
- save_clip: keep the current dashcam recording.
- ask_speed, ask_time, ask_eta: the driver asks for speed, the time, or when they will arrive.
- find_along_route: something on the way, ahead on the route. query = gas, food, coffee, rest area or hotel.
- set_timer: a countdown. when = how long ("10 minutes").
- remind: remind the driver of something later. query = what, in their words ("call mom"); when = how long from now.
- engine_codes: check-engine light, engine trouble codes, is something wrong with the truck.
- ask_fuel_economy: gas mileage, mpg. ask_gas_price: what gas or diesel costs. ask_oil_change: when the oil is due.
- ask_speed_limit: the speed limit here. ask_location: where we are, what town this is.
- more_like_this: more music like what's playing. read_news: the news or headlines.
- ask_aircraft: a plane overhead. mesh_send: send a text over the LoRa mesh; query = the message.
- help: what the driver can say.
- unknown: anything else, or nothing actionable.
Fill query and when with "" when they don't apply.
Examples: "anywhere to eat coming up" is find_along_route food. "what town is this" is ask_location.
"what plane is that up there" is ask_aircraft. "put on something similar" is more_like_this.
"wake me in ten minutes" is set_timer with when "10 minutes".
Reply with JSON only."""

SCHEMA = {"type": "object", "properties": {"action": {"type": "string", "enum": ACTIONS},
                                           "query": {"type": "string", "maxLength": 120},
                                           "when": {"type": "string", "maxLength": 40}},
          "required": ["action", "query", "when"], "additionalProperties": False}

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
    raw = _complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}], 90, timeout, llm,
                    response_format={"type": "json_schema", "json_schema": {"name": "command", "schema": SCHEMA}})
    try:
        got = json.loads(raw)
    except ValueError:
        got = {}
    action = got.get("action") if got.get("action") in ACTIONS else "unknown"
    return {"action": action, "query": str(got.get("query", "")).strip(), "when": str(got.get("when", "")).strip(),
            "llm_s": round(time.time() - t, 2)}


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


# The assistant changing the interface for the driver (app.js uiPlan runs what comes back, only from its own whitelist)
UI_DO = ["setting", "brightness", "volume", "show", "layer", "map", "listener", "run"]
UI_SYSTEM = """You turn a request to change a car head unit's screen or settings into a short list of changes.
Each change has do, key and value:
- setting: key and value, one of: units (imperial, metric); theme (auto, dark, light); clock (12, 24); orient (heading, north: the map while navigating);
  prompts (on, off: spoken directions); accent (blue, teal, green, orange, red, purple, pink, album); viz (bars, mirror, wave, scope, ring, auto, off: the music visualizer; auto = rotate);
  vizColor (album, accent, rainbow); speedWarn (show, say, off); notify (show, hide, off: phone notifications).
- brightness: key day, night, current or auto. value a number 5-100, or +20 or -20 for brighter or dimmer; for auto, on or off.
- volume: key level (value 0-100), step (value +10 or -10), or mute (value on, off or toggle).
- show: open a screen. key one of home, media, weather, news, radio, assistant, vehicle, settings, emergency, help. For radio, value may be one of fm, am, noaa, scanner, sats, air, ships, mesh, sdr (Listening); else "".
- layer: a map layer. key one of satellite, radar, hillshade, relief, contours, 3d, tsunami, quakes, aircraft, ships. value on, off or toggle.
- map: key zoom_in, zoom_out, recenter or panel (value show or hide: the side panel).
- listener: the SDR radio listening. key one of ais (ships), tpms (tire sensors), nwr (emergency broadcasts), acars (aircraft messages), adsb (aircraft). value on or off.
- run: anything else the driver can do or ask by voice, as the words they would say: navigating, music, timers, and the questions the unit answers about this truck and trip (how far can I go, any ships nearby, what's my tire pressure, any emergency alerts, will it rain, what's the speed limit). key "", value the words ("play something chill", "navigate home", "set a timer for 10 minutes", "any ships nearby").
Rules: answer with the changes the request asks for and nothing more. General chat, or a question about anything other than this unit, truck and trip, gives an empty list.
Keep key and value exactly as written above. "make it darker" or "dimmer" is brightness current -20; "dark mode" is setting theme dark.
Use the current state when a request depends on it ("toggle the radar", "a bit louder").
Examples: "switch to metric" -> setting units metric. "night mode" -> setting theme dark. "turn the volume down a bit" -> volume step -10.
"show me the aircraft" -> show radio air, and layer aircraft on. "turn off the tire listening" -> listener tpms off.
"open the weather" -> show weather. "full brightness" -> brightness current 100. "mute" -> volume mute on.
Reply with JSON only."""
UI_SCHEMA = {"type": "object", "properties": {"changes": {"type": "array", "maxItems": 4, "items": {
    "type": "object", "properties": {"do": {"type": "string", "enum": UI_DO}, "key": {"type": "string", "maxLength": 24},
                                     "value": {"type": "string", "maxLength": 80}},
    "required": ["do", "key", "value"], "additionalProperties": False}}}, "required": ["changes"], "additionalProperties": False}


def ui_plan(text, state=None, timeout=25, llm=LLM):
    """{"changes": [{"do", "key", "value"}], "llm_s"} for a request to change the interface. app.js checks every change
    against its own whitelist before doing anything, so this only proposes. Raises OSError when llama-server is down."""
    t = time.time()
    user = f"Current state: {json.dumps(state or {}, separators=(',', ':'))[:700]}\nRequest: {text}"
    raw = _complete([{"role": "system", "content": UI_SYSTEM}, {"role": "user", "content": user}], 160, timeout, llm,
                    response_format={"type": "json_schema", "json_schema": {"name": "changes", "schema": UI_SCHEMA}})
    try:
        got = json.loads(raw).get("changes") or []
    except (ValueError, AttributeError):
        got = []
    changes = [{"do": str(c.get("do")), "key": str(c.get("key", "")).strip()[:24], "value": str(c.get("value", "")).strip()[:80]}
               for c in got if isinstance(c, dict) and c.get("do") in UI_DO][:4]
    return {"changes": changes, "llm_s": round(time.time() - t, 2)}


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


WARMERS = []  # more prompts to cache at start (server.py adds the music vibe prompt)


def warm_loop():
    """Once llama-server is up, cache both system prompts before the driver speaks."""
    while True:
        try:
            command("Pause.", timeout=120)
            _complete([{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": "Hi"}], 1, 120, LLM)
            for warm in WARMERS:  # other prompts that want to be ready (music.py's vibe mixes)
                try:
                    warm()
                except Exception:  # noqa: BLE001  a warm-up is never worth failing over
                    pass
            return
        except OSError:
            time.sleep(10)
