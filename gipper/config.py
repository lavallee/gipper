"""Paths, settings and the one secret.

The Runpod key comes from the environment or gipper's .env (mode 600, git-ignored)
and only ever travels in an Authorization header: never logged, printed, written
elsewhere or passed on a command line. `redact()` scrubs it from any text that
might be shown or recorded.
"""
import os
import tomllib
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("GIPPER_DATA", HOME / ".local/share/gipper"))
CONFIG_FILE = Path(os.environ.get("GIPPER_CONFIG", HOME / ".config/gipper/config.toml"))
KEY_DIR = Path(os.environ.get("GIPPER_KEYS", HOME / ".config/gipper"))

DEFAULTS = {
    "daily_cap_usd": 5.0,           # refuse a new pod if today's spend + estimate exceeds this
    "default_job_cap_usd": 2.0,     # a job without its own budget gets this
    "default_max_hours": 3.0,
    "image": "runpod/base:1.4.1-cuda1300-ubuntu2404",
    "cuda_versions": [],            # no host filter: it empties the market; bootstrap picks a matching torch build
    "cloud": "COMMUNITY",           # COMMUNITY or SECURE
    "container_disk_gb": 40,
    "checkpoint_every_s": 300,
}


def settings():
    s = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        s.update(tomllib.loads(CONFIG_FILE.read_text()))
    return s


def _dotenv(path):
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def runpod_key():
    key = os.environ.get("RUNPOD_API_KEY") or _dotenv(Path(os.environ.get("GIPPER_ENV", ROOT / ".env"))).get("RUNPOD_API_KEY")
    if not key:
        raise SystemExit("RUNPOD_API_KEY is not set (environment or gipper/.env)")
    return key


def redact(text):
    """Remove the key from text before it is shown or recorded."""
    try:
        key = runpod_key()
    except SystemExit:
        return text
    return text.replace(key, "<redacted>") if key and isinstance(text, str) else text
