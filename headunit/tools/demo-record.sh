#!/bin/sh
# Records the captioned, private tour of the head unit (demo_tour.py) on the Pi, then puts everything back.
#   sh /opt/headunit/tools/demo-record.sh [out.mkv]        (as the desktop user; it uses sudo for the services)
# It temporarily: starts the head unit with HEADUNIT_SIM=1 (the tour uses a simulated position, never the GPS) and restarts the kiosk
# browser with a local-only DevTools port so the tour can drive it. A trap undoes both even if the tour fails.
OUT=${1:-$HOME/nomad-demo.mkv}
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
DROPIN=/etc/systemd/system/headunit.service.d/demo-sim.conf

kiosk() {  # restart the kiosk browser, with any extra arguments for it
    pkill -TERM -x chromium
    sleep 3
    for p in $(pgrep -f "[h]eadunit-kiosk"); do kill "$p" 2>/dev/null; done
    sleep 1
    nohup setsid /opt/headunit/headunit-kiosk.sh "$@" >/tmp/kiosk.log 2>&1 < /dev/null &
}

cleanup() {
    sudo -n rm -f "$DROPIN"
    sudo -n systemctl daemon-reload
    sudo -n systemctl restart headunit
    sleep 8
    kiosk
}
trap cleanup EXIT

command -v wf-recorder >/dev/null || sudo -n apt-get install -y -qq wf-recorder
sudo -n mkdir -p "$(dirname "$DROPIN")"
printf '[Service]\nEnvironment=HEADUNIT_SIM=1\n' | sudo -n tee "$DROPIN" >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl restart headunit
sleep 10
kiosk --remote-debugging-port=9222
until curl -sf -o /dev/null http://127.0.0.1:9222/json; do sleep 2; done
sleep 25   # the page and its map settle
python3 /opt/headunit/tools/demo_tour.py --out "$OUT"
