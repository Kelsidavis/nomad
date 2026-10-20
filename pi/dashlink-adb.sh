#!/bin/sh
# Keeps the phone reachable at localhost:8780 on the Pi over USB, across unplugs and phone reboots.
# Needs: apt install adb, and the phone authorized once for this Pi ("Always allow" on the prompt).
PORT=8780

while true; do
    adb wait-for-device
    adb forward tcp:$PORT tcp:$PORT
    # Opening the app starts the service if Android killed it or it hasn't started since boot.
    if ! curl -sf -m 3 "http://127.0.0.1:$PORT/" >/dev/null; then
        adb shell am start -n dev.dashlink/.MainActivity >/dev/null 2>&1
    fi
    # Re-check every 10s; a replug drops the forward, and the loop puts it back.
    while adb get-state >/dev/null 2>&1 && adb forward --list | grep -q "tcp:$PORT"; do
        sleep 10
    done
done
