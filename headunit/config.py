"""NOMAD's settings: the environment first (systemd loads /etc/nomad.conf), then nomad.conf at the top of the repo,
then /etc/nomad.conf. nomad.conf is KEY=VALUE lines (shell syntax, so setup-pi.sh and deploy.sh can source it);
copy nomad.conf.example to start one. It's gitignored: everyone keeps their own.
"""
import os
import pathlib

FILES = [pathlib.Path(__file__).resolve().parent.parent / "nomad.conf", pathlib.Path("/etc/nomad.conf")]
DEFAULTS = {"NOMAD_NAME": "Truck"}


def _read(path):
    out = {}
    try:
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


_file = {}
for _p in reversed(FILES):  # the repo's nomad.conf wins over /etc's
    _file.update(_read(_p))


def get(key, default=None):
    v = os.environ.get(key) or _file.get(key) or DEFAULTS.get(key) or default
    return os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v


def home():
    """(lat, lon) from HEADUNIT_HOME="lat,lon", or None."""
    v = get("HEADUNIT_HOME")
    try:
        lat, lon = (float(x) for x in v.split(","))
        return lat, lon
    except (AttributeError, ValueError):
        return None


NAME = get("NOMAD_NAME")  # the Bluetooth name, and the word that starts a voice command ("Truck, ...")
