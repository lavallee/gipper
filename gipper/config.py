"""Paths, settings and the provider keys.

Each provider key (RUNPOD_API_KEY, VAST_API_KEY, SHADEFORM_API_KEY) comes from the
environment or gipper's .env (mode 600, git-ignored) and only ever travels in a
request header: never logged, printed, written elsewhere or passed on a command
line. `redact()` scrubs every one of them from any text that might be shown or
recorded.
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
    "image": "runpod/base:1.4.1-ubuntu2404",   # Runpod's default; no CUDA in the image: torch wheels bring their own
    "vast_image": "vastai/base-image:@vastai-automatic-tag",   # Vast picks the tag that fits the host
    "providers": ["runpod", "vast", "shadeform"],   # markets to shop; those without a key are skipped
    "regions": [],                  # ISO country codes a job may run in by default; empty = anywhere
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


KEYS = ("RUNPOD_API_KEY", "VAST_API_KEY", "SHADEFORM_API_KEY")


def secret(name, required=True):
    key = os.environ.get(name) or _dotenv(Path(os.environ.get("GIPPER_ENV", ROOT / ".env"))).get(name)
    if not key and required:
        raise SystemExit(f"{name} is not set (environment or gipper/.env)")
    return key or None


def runpod_key():
    return secret("RUNPOD_API_KEY")


def redact(text):
    """Remove every provider key from text before it is shown or recorded."""
    if not isinstance(text, str):
        return text
    for name in KEYS:
        key = secret(name, required=False)
        if key:
            text = text.replace(key, "<redacted>")
    return text
