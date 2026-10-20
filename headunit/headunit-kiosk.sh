#!/bin/sh
# Shows the head unit full-screen once the backend answers. Started from the desktop session
# (~/.config/autostart/headunit.desktop). If Chromium crashes it comes back by itself; closing it on purpose
# (Alt+F4) ends the kiosk. To stop it from a shell: pkill -f headunit-kiosk; pkill -f headunit-chromium
URL=${HEADUNIT_URL:-http://127.0.0.1:8800/}

wait_for_server() {
    for _ in $(seq 1 60); do
        curl -sf -o /dev/null "$URL" && return
        sleep 1
    done
}

while :; do
    wait_for_server
    # A profile of its own, so crash bubbles and the normal browser's tabs never show up here.
    chromium --kiosk "$URL" \
        --user-data-dir="$HOME/.config/headunit-chromium" \
        --ozone-platform=wayland \
        --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
        --disable-features=TranslateUI,Translate,HardwareMediaKeyHandling \
        --overscroll-history-navigation=0 --disable-pinch \
        --autoplay-policy=no-user-gesture-required \
        --password-store=basic --check-for-update-interval=31536000 "$@"
    status=$?
    [ "$status" -eq 0 ] && exit 0  # closed on purpose
    echo "headunit-kiosk: chromium exited with $status, restarting" >&2
    sleep 3
done
