#!/bin/sh
# Shows the head unit full-screen once the backend answers. Started from the desktop session
# (~/.config/autostart/headunit.desktop). Exit kiosk with Alt+F4, or: pkill -f headunit-chromium
URL=${HEADUNIT_URL:-http://127.0.0.1:8800/}

for _ in $(seq 1 60); do
    curl -sf -o /dev/null "$URL" && break
    sleep 1
done

# A profile of its own, so crash bubbles and the normal browser's tabs never show up here.
exec chromium --kiosk "$URL" \
    --user-data-dir="$HOME/.config/headunit-chromium" \
    --ozone-platform=wayland \
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
    --disable-features=TranslateUI,Translate,HardwareMediaKeyHandling \
    --overscroll-history-navigation=0 --disable-pinch \
    --autoplay-policy=no-user-gesture-required \
    --password-store=basic --check-for-update-interval=31536000 "$@"
