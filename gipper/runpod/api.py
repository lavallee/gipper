"""Old import path for the Runpod client: `from gipper.runpod.api import Api`.
Guards and spend collectors written before 2026-10-26 keep working; new code uses
`gipper.providers.runpod.Runpod().api()` or `gipper.providers.load(name)`."""
from gipper.config import runpod_key
from gipper.providers.runpod import Api as _Api, RunpodError  # noqa: F401


class Api(_Api):
    def __init__(self, key=None, timeout=60):
        super().__init__(key or runpod_key())
