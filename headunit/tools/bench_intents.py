#!/usr/bin/env python3
"""Score small LLMs on head-unit voice commands through a running llama-server (OpenAI API).

    llama-server -m model.gguf -c 2048 -t 4 --port 8090 &
    bench_intents.py --port 8090 --name qwen3.5-0.8b

Each utterance gets the same system prompt and a JSON schema, so output is always parseable; the score is
whether the action (and its argument) is what the driver meant. Uses ../intent.py, so it tests
the prompt the car runs. Stdlib only.
"""
import argparse
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import intent  # noqa: E402  (the prompt and schema the car uses)


# (utterance as whisper would transcribe it, expected action, words the query must contain)
TESTS = [
    ("Navigate to the nearest gas station.", "navigate", ["gas"]),
    ("Take me to Safeway.", "navigate", ["safeway"]),
    ("I need gas.", "navigate", ["gas"]),
    ("Find me a pizza place.", "navigate", ["pizza"]),
    ("Directions to Mount St. Helens.", "navigate", ["helens"]),
    ("Get me to the Portland airport.", "navigate", ["portland", "airport"]),
    ("Navigate to star bucks.", "navigate", ["star"]),
    ("I'm hungry, find a burger place.", "navigate", ["burger"]),
    ("Where's the closest hospital?", "navigate", ["hospital"]),
    ("Stop navigating.", "cancel_navigation", []),
    ("Cancel the route.", "cancel_navigation", []),
    ("Play some Elvis.", "play_music", ["elvis"]),
    ("Put on Fleetwood Mac.", "play_music", ["fleetwood"]),
    ("Hey, can you play Thriller by Michael Jackson?", "play_music", ["thriller"]),
    ("Play music.", "play_music", []),
    ("Pause.", "pause", []),
    ("Skip this song.", "next_track", []),
    ("Go back a song.", "previous_track", []),
    ("Turn it up.", "volume_up", []),
    ("It's too loud.", "volume_down", []),
    ("Zoom out a bit.", "zoom_out", []),
    ("Show me the cameras.", "show_screen", ["camera"]),
    ("Open settings.", "show_screen", ["setting"]),
    ("Save that clip.", "save_clip", []),
    ("Did you see that? Keep the dashcam footage.", "save_clip", []),
    ("How fast am I going?", "ask_speed", []),
    ("What time is it?", "ask_time", []),
    ("When will we get there?", "ask_eta", []),
    ("Tell me a joke.", "unknown", []),
    ("Uh, never mind.", "unknown", []),
]


def ask(port, text):
    t = time.time()
    got = intent.command(text, timeout=120, llm=f"http://127.0.0.1:{port}")
    return json.dumps(got), time.time() - t, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--name", default="model")
    args = ap.parse_args()
    ask(args.port, "Pause.")  # warm up: loads the system prompt into the cache
    ok, times, rows = 0, [], []
    for text, want, words in TESTS:
        raw, dt, _ = ask(args.port, text)
        times.append(dt)
        try:
            got = json.loads(raw)
        except ValueError:
            got = {"action": "?", "query": raw}
        q = re.sub(r"[^a-z ]", "", str(got.get("query", "")).lower())
        hit = got.get("action") == want and all(w in q for w in words)
        ok += hit
        rows.append(f"  {'✓' if hit else '✗'} {dt:4.2f}s  {text:48} -> {got.get('action')} {got.get('query')!r}")
    times.sort()
    print(f"{args.name}: {ok}/{len(TESTS)} correct, median {times[len(times) // 2]:.2f}s, "
          f"worst {times[-1]:.2f}s")
    print("\n".join(r for r in rows if "✗" in r))
    print(json.dumps({"name": args.name, "correct": ok, "total": len(TESTS),
                      "median_s": round(times[len(times) // 2], 2), "max_s": round(times[-1], 2)}))


if __name__ == "__main__":
    main()
