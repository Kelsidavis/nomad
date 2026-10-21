"""Writes that survive losing power: the new content goes to a temporary file, is flushed to the disk, and only then replaces
the old file (and the directory entry is flushed too). A power cut leaves either the old file or the new one, never a half-written
or empty file, which is what an ordinary write followed by a cut can leave behind on this disk (it happened to /etc/nomad.conf).
"""
import os
import pathlib


def write_text(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
