#!/bin/sh
# NOMAD network watchdog (netwatch.service). The Pi's Wi-Fi can stall without saying so: NetworkManager still
# reports "connected" but nothing gets through. When connected over Wi-Fi and the router stops answering for about
# two minutes, reconnect; if that doesn't bring it back, reload the Wi-Fi driver. With no connection (the truck out
# of range of anything) it does nothing.
fails=0
while :; do
    sleep 30
    gw=$(ip -4 route show default dev wlan0 2>/dev/null | awk '/default/ {print $3; exit}')
    [ -n "$gw" ] || { fails=0; continue; }
    if ping -c 2 -W 3 -I wlan0 "$gw" >/dev/null 2>&1; then
        fails=0
        continue
    fi
    fails=$((fails + 1))
    [ "$fails" -lt 4 ] && continue
    con=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2 == "wlan0" {print $1; exit}')
    if [ "$fails" -eq 4 ]; then
        echo "netwatch: router $gw not answering for 2 min on '$con': reconnecting Wi-Fi"
        nmcli dev disconnect wlan0 >/dev/null 2>&1
        sleep 2
        [ -n "$con" ] && nmcli con up "$con" >/dev/null 2>&1 || nmcli dev connect wlan0 >/dev/null 2>&1
    elif [ "$fails" -eq 8 ]; then
        echo "netwatch: still no router after reconnecting: reloading the Wi-Fi driver"
        modprobe -r brcmfmac_wcc brcmfmac 2>/dev/null; sleep 3; modprobe brcmfmac
        fails=0
    fi
done
