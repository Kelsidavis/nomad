#!/bin/sh
# Send this working copy to the Pi in nomad.conf (PI_HOST) and restart the head unit: edit here, deploy, test.
#   ./deploy.sh            code only (UI, server, tools), restarts headunit
#   ./deploy.sh --all      also the speech/LLM/routing service units and /etc/nomad.conf (asks for sudo)
set -e
cd "$(dirname "$0")"
[ -f nomad.conf ] || { echo "No nomad.conf: copy nomad.conf.example to nomad.conf and set PI_HOST." >&2; exit 1; }
. ./nomad.conf
: "${PI_HOST:?set PI_HOST in nomad.conf}"
echo "== $PI_HOST"
rsync -rlt --delete --exclude __pycache__ --exclude '*.pyc' \
    headunit/*.py \
    headunit/tts_server.py headunit/headunit-kiosk.sh headunit/btaudio.sh headunit/static "$PI_HOST:/opt/headunit/"
rsync -rlt headunit/tools/ "$PI_HOST:/opt/headunit/tools/" --exclude __pycache__
rsync -rlt pi/*.py "$PI_HOST:/opt/dashlink/"
# which version is on the Pi (Settings → System shows it)
printf '%s %s\n' "$(git describe --always --dirty 2>/dev/null || echo unknown)" "$(date '+%Y-%m-%d %H:%M')" | ssh "$PI_HOST" 'cat > /opt/headunit/VERSION'
if [ "$1" = "--all" ]; then
    scp -q nomad.conf headunit/*.service "$PI_HOST:/tmp/"
    # The units name user k and /home/k; on the Pi they become whoever PI_HOST logs in as.
    ssh -t "$PI_HOST" 'H=$HOME U=$(id -un); sudo sh -c "install -m 644 /tmp/nomad.conf /etc/nomad.conf &&
        for s in /tmp/*.service; do sed \"s#/home/k/#$H/#g; s/^User=k\$/User=$U/\" \"\$s\" > /etc/systemd/system/\$(basename \$s); done &&
        systemctl daemon-reload && systemctl restart headunit llm stt tts btaudio"'
else
    ssh -t "$PI_HOST" 'sudo systemctl restart headunit'
fi
echo "== checking"
sleep 6
ssh "$PI_HOST" 'python3 /opt/headunit/tools/selftest.py --quick' || echo "(some checks failed: see above)"
echo "== done: reload the screen (or it reloads itself when the server comes back)"
