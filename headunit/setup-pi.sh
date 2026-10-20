#!/bin/sh
# Sets up a fresh Raspberry Pi OS (trixie) card as the head unit. Safe to run again.
# Copy the repo's pi/ and headunit/ folders to the Pi, then:  sudo sh headunit/setup-pi.sh
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
# Your settings (copy nomad.conf.example to nomad.conf); the services read them from /etc/nomad.conf.
[ -f "$REPO/nomad.conf" ] && . "$REPO/nomad.conf" && install -m 644 "$REPO/nomad.conf" /etc/nomad.conf
U=${SUDO_USER:-k}
H=$(getent passwd "$U" | cut -d: -f6)
UIDN=$(id -u "$U")
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -qq
apt-get install -y -qq adb rclone fuse3 gpsd gpsd-clients chrony docker.io alsa-utils rtl-sdr satdump ffmpeg python3-venv fonts-noto-color-emoji >/dev/null

echo "== SDR: keep the kernel's DVB-T TV driver off the dongle so rtl-sdr can use it"
printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' > /etc/modprobe.d/rtl-sdr-blacklist.conf

echo "== mic HAT (WM8960)"
C=/boot/firmware/config.txt
grep -q '^dtoverlay=wm8960-soundcard' $C || printf '\n[all]\ndtparam=i2c_arm=on\ndtparam=i2s=on\ndtoverlay=wm8960-soundcard\n' >> $C

echo "== DashLink (optional phone link: cameras, dashcam, phone storage; installed but not enabled)"
# server.py imports dashlink.py, so it is installed either way. To use the phone:
#   sudo systemctl enable --now dashlink-adb dashcam phone-storage
mkdir -p /opt/dashlink
install -m 644 "$REPO"/pi/*.py /opt/dashlink/
install -m 755 "$REPO"/pi/dashlink-adb.sh /usr/local/bin/
for f in "$REPO"/pi/*.service; do sed "s/^User=pi$/User=$U/" "$f" > /etc/systemd/system/"$(basename "$f")"; done
grep -q '^user_allow_other' /etc/fuse.conf || echo user_allow_other >> /etc/fuse.conf
usermod -aG plugdev,audio,gpio,dialout "$U"

echo "== GPS (gpsd) and GPS time (chrony)"
# A USB GPS puck behind a CP210x serial bridge (NMEA, 9600 baud), handed to gpsd whenever it's plugged in.
# Debian leaves the generic CP210x rule off because many unrelated adapters use that chip, so the rule matches the
# serial of the GPS plugged in now (GPS_SERIAL=... to choose; with none, any CP210x).
GPS_SERIAL=${GPS_SERIAL:-$(for d in /dev/serial/by-id/usb-Silicon_Labs_CP210*; do [ -e "$d" ] && udevadm info -q property -n "$d" | sed -n 's/^ID_SERIAL_SHORT=//p'; done | head -n 1)}
{
  echo "# The head unit's USB GPS: hand it to gpsd whenever it is plugged in (written by setup-pi.sh)."
  echo "SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"10c4\", ATTRS{idProduct}==\"ea60\",${GPS_SERIAL:+ ATTRS{serial}==\"$GPS_SERIAL\",} SYMLINK+=\"gps0\", TAG+=\"systemd\", ENV{SYSTEMD_WANTS}=\"gpsdctl@%k.service\""
} > /etc/udev/rules.d/61-headunit-gps.rules
udevadm control --reload
cat > /etc/default/gpsd <<'EOT'
# -n keeps it polling with no clients; the udev rule adds the GPS (DEVICES stays empty so it isn't opened twice).
DEVICES=""
GPSD_OPTIONS="-n -s 9600"
USBAUTO="true"
START_DAEMON="true"
EOT
cat > /etc/chrony/conf.d/gps.conf <<'EOT'
# GPS time from gpsd shared memory (NMEA only: ~100 ms jitter). Used when offline; internet NTP wins when online.
# No "trust": a bogus GPS date (week rollover) is rejected when other sources disagree.
refclock SHM 0 refid NMEA offset 0.15 delay 0.2
# Step the clock only during the first 3 updates after boot, then slew.
makestep 1 3
EOT

echo "== head unit UI"
mkdir -p /opt/headunit
cp -r "$REPO"/headunit/server.py "$REPO"/headunit/offline.py "$REPO"/headunit/intent.py "$REPO"/headunit/tts_server.py "$REPO"/headunit/radio.py "$REPO"/headunit/bt.py "$REPO"/headunit/config.py "$REPO"/headunit/emergency.py "$REPO"/headunit/network.py "$REPO"/headunit/music.py "$REPO"/headunit/news.py "$REPO"/headunit/obd.py "$REPO"/headunit/mesh_bridge.py "$REPO"/headunit/trip.py "$REPO"/headunit/adsb.py "$REPO"/headunit/fuel.py "$REPO"/headunit/drives.py "$REPO"/headunit/static "$REPO"/headunit/headunit-kiosk.sh /opt/headunit/
chmod 755 /opt/headunit/headunit-kiosk.sh
mkdir -p /opt/headunit/tools && cp "$REPO"/headunit/tools/sat_passes.py "$REPO"/headunit/tools/sstv_decode.py "$REPO"/headunit/tools/fetch_emergency.py "$REPO"/headunit/tools/bench_intents.py /opt/headunit/tools/
# satellite passes and SSTV pictures for the radio tab: sgp4 and sstv in their own venv
[ -x "$H/.venvs/sat/bin/python" ] || sudo -u "$U" sh -c 'python3 -m venv ~/.venvs/sat && ~/.venvs/sat/bin/pip install -q sgp4==2.24 sstv==0.2.0'
chown -R "$U:$U" /opt/headunit  # so code updates need no sudo
sed "s/^User=k$/User=$U/; s#/run/user/1000#/run/user/$UIDN#" "$REPO"/headunit/headunit.service > /etc/systemd/system/headunit.service
sed "s#/home/k/#$H/#g" "$REPO"/headunit/valhalla.service > /etc/systemd/system/valhalla.service
# Voice model: needs llama.cpp built in ~/llama.cpp and the model in ~/models (see README); idle until then.
sed "s#/home/k/#$H/#g; s/^User=k$/User=$U/" "$REPO"/headunit/llm.service > /etc/systemd/system/llm.service
# Speech on the Pi: whisper.cpp built in ~/whisper.cpp with ~/models/ggml-base.en-q5_1.bin (see README), and the
# Kokoro voice (Bella) in ~/.venvs/kokoro with its model in ~/models/kokoro.
if [ ! -s "$H/models/kokoro/voices-v1.0.bin" ]; then
    sudo -u "$U" sh -c 'python3 -m venv ~/.venvs/kokoro && ~/.venvs/kokoro/bin/pip install -q kokoro-onnx==0.6.1 && mkdir -p ~/models/kokoro && cd ~/models/kokoro &&
        for f in kokoro-v1.0.int8.onnx voices-v1.0.bin; do curl -fsSL -o $f https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f; done'
fi
# LoRa: the meshtastic library for mesh_bridge.py (mesh.service), in ~/.venvs/mesh
[ -x "$H/.venvs/mesh/bin/python" ] || sudo -u "$U" sh -c 'python3 -m venv ~/.venvs/mesh && ~/.venvs/mesh/bin/pip install -q meshtastic'
for s in stt tts mesh; do sed "s#/home/k/#$H/#g; s/^User=k$/User=$U/" "$REPO"/headunit/$s.service > /etc/systemd/system/$s.service; done
install -o "$U" -g "$U" -d "$H/.config/autostart" "$H/.local/share/headunit"
rm -f "$H/.config/autostart/headunit.desktop"  # the NOMAD session's labwc autostart starts the kiosk now

echo "== offline routing image (Valhalla)"
docker image inspect ghcr.io/valhalla/valhalla:3.9.1 >/dev/null 2>&1 || docker pull -q ghcr.io/valhalla/valhalla:3.9.1

echo "== Wi-Fi from Settings: the netdev group may manage networks without a password"
usermod -aG netdev "$U"
cat > /etc/polkit-1/rules.d/50-nomad-network.rules <<'EOT'
// NOMAD: the head unit (its user is in netdev) picks Wi-Fi networks from its Settings screen.
polkit.addRule(function (action, subject) {
  if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 && subject.isInGroup("netdev")) return polkit.Result.YES;
});
EOT

echo "== boot screen and no desktop: the NOMAD splash, quiet boot, and a session that is just the head unit"
mkdir -p /usr/share/plymouth/themes/nomad && cp "$REPO"/headunit/boot/nomad/* /usr/share/plymouth/themes/nomad/
grep -q '^disable_splash=1' "$C" || printf '\n[all]\n# NOMAD: no rainbow square at power-on\ndisable_splash=1\n' >> "$C"
L=/boot/firmware/cmdline.txt
c=$(cat "$L"); for opt in quiet splash loglevel=3 logo.nologo vt.global_cursor_default=0; do
    case " $c " in *" $opt "*) ;; *) c="$c $opt";; esac; done; echo "$c" > "$L"
plymouth-set-default-theme -R nomad >/dev/null 2>&1 || true
install -m 755 "$REPO"/headunit/boot/nomad-session /usr/local/bin/nomad-session
install -m 644 "$REPO"/headunit/boot/nomad.desktop /usr/share/wayland-sessions/nomad.desktop
install -o "$U" -g "$U" -d "$H/.config/labwc"
[ -f "$H/.config/labwc/rc.xml" ] || install -o "$U" -g "$U" -m 644 /etc/xdg/labwc/rc.xml "$H/.config/labwc/rc.xml" 2>/dev/null || true
install -o "$U" -g "$U" -m 644 "$REPO"/headunit/labwc-autostart "$H/.config/labwc/autostart"
sed -i "s/^#\?autologin-user=.*/autologin-user=$U/; s/^#\?autologin-session=.*/autologin-session=nomad/; s/^#\?user-session=.*/user-session=nomad/" /etc/lightdm/lightdm.conf

echo "== watchdog (a frozen Pi reboots itself) and a small log kept across reboots"
mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d /var/log/journal
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' > /etc/systemd/system.conf.d/nomad-watchdog.conf
printf '[Journal]\nStorage=persistent\nSystemMaxUse=30M\nSyncIntervalSec=30s\n' > /etc/systemd/journald.conf.d/nomad.conf

echo "== faster boot"
# Nothing here needs the internet, and in the car there is usually no Wi-Fi: waiting for it held up Docker
# (and so offline routing) at every boot. cloud-init only matters on the first boot; packagekit is desktop updates.
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
touch /etc/cloud/cloud-init.disabled
systemctl mask packagekit.service >/dev/null
# SD card first, USB as a fallback; no NVMe probe or network install at power-on.
if command -v rpi-eeprom-config >/dev/null && ! rpi-eeprom-config | grep -q '^BOOT_ORDER=0xf41$'; then
    rpi-eeprom-config | sed 's/^BOOT_ORDER=.*/BOOT_ORDER=0xf41/; s/^NET_INSTALL_AT_POWER_ON=.*/NET_INSTALL_AT_POWER_ON=0/' > /tmp/eeprom.txt
    rpi-eeprom-config --apply /tmp/eeprom.txt
fi

systemctl daemon-reload
systemctl enable gpsd.socket gpsd chrony headunit valhalla llm stt tts mesh >/dev/null 2>&1
systemctl restart gpsd chrony headunit
systemctl start valhalla || true  # waits for the routing data
systemctl start llm stt tts || true  # wait for their models
echo "== done"
systemctl is-active gpsd chrony headunit valhalla llm stt tts | paste -sd' '
