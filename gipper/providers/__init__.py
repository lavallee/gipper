"""GPU markets gipper can rent from. Each provider answers the same questions:

    offers(job)                      -> [Offer] that fit (memory, regions, ports)
    create(offer, job, pubkey, name) -> {"id", "usd_h", "gpu"}; raises Unavailable if
                                        the offer is gone, so the next one is tried
    wait_ready(id, job)              -> (Remote, {"machine", "gpu", "ports"}); raises
                                        BadHost if the machine will never come up
    terminate(id)
    list_ours()                      -> [{"id", "name", "started"}] for names gipper-*
    account()                        -> one line on balance and spend
    billed(since)                    -> {id: (usd, detail)} actually charged, or None if
                                        the provider can't say

Everything else (caps, upload, the detached run, checkpoints, teardown, receipts)
is the same for every provider and lives in gipper.run.
"""
from importlib import import_module

from gipper.config import secret

NAMES = ("runpod", "vast", "shadeform")


class Unavailable(RuntimeError):
    """The offer was taken or the market has nothing free: try the next offer."""


class BadHost(RuntimeError):
    """The machine is busy, broken or never came up: end it and rent another."""


class Provider:
    name = ""
    key_name = ""
    udp = False

    def has_key(self):
        return bool(secret(self.key_name, required=False))

    def key(self):
        return secret(self.key_name)

    def billed(self, since):
        return None


def load(name):
    if name not in NAMES:
        raise KeyError(f"{name}: not a provider gipper knows ({', '.join(NAMES)})")
    return import_module(f"gipper.providers.{name}").PROVIDER
