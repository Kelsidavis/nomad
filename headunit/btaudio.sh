#!/bin/sh
# NOMAD Bluetooth speaker keeper (btaudio.service). Keeps the amp or speaker named by BT_AUDIO in nomad.conf connected
# and makes it the default output, so nothing has to be re-paired or reconnected by hand. Bluetooth amps drop the link
# when idle, power-cycle with the ignition, or leave the Pi believing it is still connected; this notices all three.
# Not connected: scan briefly (many amps only answer while the Pi is scanning), then connect. "Connected" but no audio
# sink for a minute: a stale link, so drop it and start over. Connected: make it the default output if it isn't.
# With BT_AUDIO unset it does nothing.
[ -n "$BT_AUDIO" ] || { echo "btaudio: BT_AUDIO not set in nomad.conf; nothing to keep connected" >&2; exec sleep infinity; }
addr=$(echo "$BT_AUDIO" | tr 'a-f' 'A-F')

# The PipeWire sink for the amp: "<node id> <is it the default>", or nothing while it has none.
sink() {
    pw-dump 2>/dev/null | python3 -c '
import json, sys
addr = sys.argv[1]
objs = json.load(sys.stdin)
default = ""
for o in objs:
    if o.get("type") == "PipeWire:Interface:Metadata":
        for m in o.get("metadata", []):
            if m.get("key") == "default.audio.sink":
                default = (m.get("value") or {}).get("name", "")
for o in objs:
    p = o.get("info", {}).get("props", {})
    if p.get("media.class") == "Audio/Sink" and p.get("api.bluez5.address", "").upper() == addr:
        print(o["id"], "yes" if p.get("node.name") == default else "no")
        break
' "$addr"
}

bluetoothctl power on >/dev/null 2>&1
bluetoothctl trust "$addr" >/dev/null 2>&1
nosink=0
while :; do
    if bluetoothctl info "$addr" 2>/dev/null | grep -q 'Connected: yes'; then
        set -- $(sink)
        if [ -z "$1" ]; then
            nosink=$((nosink + 1))
            if [ "$nosink" -ge 6 ]; then
                echo "btaudio: connected but no audio sink for a minute: reconnecting"
                bluetoothctl disconnect "$addr" >/dev/null 2>&1
                nosink=0
            fi
        else
            nosink=0
            if [ "$2" = no ]; then
                wpctl set-default "$1" && echo "btaudio: $addr is the default output"
            fi
        fi
    else
        nosink=0
        echo "btaudio: $addr not connected: connecting"
        bluetoothctl --timeout 6 scan on >/dev/null 2>&1
        timeout 20 bluetoothctl connect "$addr" >/dev/null 2>&1
    fi
    sleep 10
done
