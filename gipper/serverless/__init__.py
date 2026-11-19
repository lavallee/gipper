"""Queue jobs across serverless GPU hosts, independent of the Pod/SSH runner.

Adapters implement create(spec), get(id), list(), delete(id), submit(id, input,
execution_ms), status(id, job_id), cancel(id, job_id), health(id). They return
JSON-compatible dictionaries; job status is one of the Runpod-style TERMINAL
states below. Only Runpod is implemented. Images and handler inputs belong to
the application. No automatic cross-host retry: a timed-out submission may exist.
"""
import hashlib
import json
import math
import re
import time
import tomllib
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

from gipper import ledger
from gipper.config import redact

TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})


class CleanupPending(RuntimeError):
    """Closure could not be verified; do not rent a replacement."""


class SubmissionUnknown(RuntimeError):
    """Submission response was lost. Inspect the endpoint; never blindly resubmit."""


@dataclass(frozen=True)
class Spec:
    name: str
    image: str
    provider: str = "runpod"
    max_workers: int = 1
    idle_seconds: int = 15
    execution_seconds: int = 300
    session_seconds: int = 600
    rate_usd_h: float = 0.0
    max_usd: float = 1.0
    disk_gb: int = 20
    command: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        if not re.fullmatch(r"gipper-[a-zA-Z0-9_-]+", self.name):
            raise ValueError("name must start with gipper- and use letters, digits, _ or -")
        if not self.image or self.provider not in ("runpod",):
            raise ValueError("image required; only runpod serverless is implemented")
        for name in ("max_workers", "idle_seconds", "execution_seconds", "session_seconds", "disk_gb"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.idle_seconds > 3600:
            raise ValueError("idle_seconds must be <= 3600")
        if not isinstance(self.command, list) or any(not isinstance(x, str) for x in self.command):
            raise ValueError("command must be a list of strings")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
               for v in (self.rate_usd_h, self.max_usd)):
            raise ValueError("rate_usd_h and max_usd must be finite and positive")
        if self.execution_seconds > self.session_seconds:
            raise ValueError("execution_seconds exceeds session_seconds")
        if self.estimate_usd > self.max_usd:
            raise ValueError(f"session allowance ${self.estimate_usd:.4f} exceeds max_usd ${self.max_usd:.4f}")
        if self.provider == "runpod":
            from .runpod import validate_options
            validate_options(self.options)

    @property
    def estimate_usd(self):
        # Conservative occupancy allowance, NOT measured billing or a provider cap.
        return self.rate_usd_h * self.max_workers * (self.session_seconds + self.idle_seconds) / 3600


def load(path):
    data = tomllib.loads(Path(path).read_text())
    unknown = set(data) - {"endpoint", "budget", "runpod"}
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")
    return Spec(**data.get("endpoint", {}), **data.get("budget", {}), options=data.get("runpod", {}))


def adapter(name):
    if name != "runpod":
        raise ValueError(f"{name}: serverless adapter not implemented")
    return import_module(f"gipper.serverless.{name}").Adapter()


class Client:
    def __init__(self, provider="runpod", *, backend=None):
        self.provider = provider
        self.backend = backend or adapter(provider)

    def _receipt(self, kind, payload, endpoint=None, job=None):
        return ledger.write("serverless." + kind, {"provider": self.provider, **payload},
                            job=job, refs={"endpoint": endpoint} if endpoint else {})

    def _owned(self, endpoint):
        records = [r for r in ledger.read(kinds={"serverless.create"})
                   if r["payload"].get("provider") == self.provider
                   and r["payload"].get("endpoint") == endpoint]
        if not records:
            raise ValueError(f"{endpoint}: not created by this Gipper ledger")
        resource = self.backend.get(endpoint)
        if resource is not None and resource.get("name") != records[-1]["payload"]["name"]:
            raise ValueError("endpoint ownership/name changed")
        return resource

    def create(self, spec, *, temporary=False):
        pending = {}
        for record in ledger.read(kinds={"serverless.cleanup_pending", "serverless.delete", "serverless.cancel_pending"}):
            if record["payload"].get("provider") == self.provider:
                pending[record["payload"].get("endpoint") or record["refs"].get("endpoint")] = record["kind"] != "serverless.delete"
        if any(pending.values()):
            raise CleanupPending("previous endpoint closure/cancellation is unconfirmed; recover it before creating another")
        if spec.provider != self.provider:
            raise ValueError("provider mismatch")
        self._receipt("create_intent", {"name": spec.name, "image": spec.image,
                                      "allowance_usd": spec.estimate_usd, "rate_usd_h": spec.rate_usd_h})
        try:
            resource = self.backend.create(spec)
        except Exception as e:
            self._receipt("create_unknown", {"name": spec.name, "error": redact(str(e)),
                                            "action": "inspect provider by exact name before retry"})
            raise SubmissionUnknown(f"Creation unconfirmed for {spec.name}; inspect before retry") from e
        endpoint = resource["id"]
        self._receipt("create", {"endpoint": endpoint, "name": spec.name, "image": spec.image,
                                 "rate_usd_h": spec.rate_usd_h, "session_seconds": spec.session_seconds,
                                 "expires_at": time.time() + spec.session_seconds if temporary else None}, endpoint)
        try:
            verified = self.backend.get(endpoint)
            self.backend.verify(verified, spec)
        except BaseException:
            self.delete(endpoint)
            raise
        self._receipt("verified", {"endpoint": endpoint, "workers": verified.get("workers"),
                                   "timeout": verified.get("timeout")}, endpoint)
        return endpoint

    def list(self):
        owned = {r["payload"]["endpoint"] for r in ledger.read(kinds={"serverless.create"})
                 if r["payload"].get("provider") == self.provider}
        return [r for r in self.backend.list() if r["id"] in owned and r.get("name", "").startswith("gipper-")]

    def reap(self, *, dry_run=False):
        """Delete expired temporary endpoints only; deliberately deployed ones persist."""
        deadlines = {r["payload"]["endpoint"]: r["payload"].get("expires_at")
                     for r in ledger.read(kinds={"serverless.create"})
                     if r["payload"].get("provider") == self.provider}
        expired = [r["id"] for r in self.list() if deadlines.get(r["id"])
                   and deadlines[r["id"]] <= time.time()]
        for endpoint in expired:
            if not dry_run:
                self.delete(endpoint)
        return expired

    def delete(self, endpoint):
        if self._owned(endpoint) is None:
            self._receipt("delete", {"endpoint": endpoint, "verified_absent": True}, endpoint)
            return
        self._receipt("delete_intent", {"endpoint": endpoint}, endpoint)
        error = None
        try:
            self.backend.delete(endpoint)
        except Exception as e:
            error = redact(str(e))
        try:
            absent = self.backend.get(endpoint) is None
        except Exception as e:
            absent, error = False, redact(str(e))
        self._receipt("delete" if absent else "cleanup_pending", {
            "endpoint": endpoint, "verified_absent": absent, "error": error}, endpoint)
        if not absent:
            raise CleanupPending(f"{endpoint}: deletion unconfirmed; retry deletion before any replacement")

    def submit(self, endpoint, payload, execution_seconds=300):
        if not isinstance(payload, dict):
            raise ValueError("handler input must be a JSON object")
        if type(execution_seconds) is not int or execution_seconds < 1:
            raise ValueError("execution_seconds must be a positive integer")
        resource = self._owned(endpoint)
        if resource is None:
            raise ValueError("endpoint is absent")
        if execution_seconds * 1000 > resource.get("timeout", 0):
            raise ValueError("execution_seconds exceeds endpoint timeout")
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        request_id = ledger.new_id("sj")
        self._receipt("submit_intent", {"request_id": request_id, "input_sha256": hashlib.sha256(encoded).hexdigest(),
                                       "input_bytes": len(encoded)}, endpoint, request_id)
        try:
            response = self.backend.submit(endpoint, payload, execution_seconds * 1000)
            job_id = response["id"]
        except Exception as e:
            self._receipt("submit_unknown", {"error_type": type(e).__name__}, endpoint, request_id)
            raise SubmissionUnknown(f"{endpoint}: submission unconfirmed; do not resubmit automatically") from e
        self._receipt("submit", {"job_id": job_id, "request_id": request_id}, endpoint, job_id)
        return job_id

    def status(self, endpoint, job_id):
        self._job_owned(endpoint, job_id)
        return self.backend.status(endpoint, job_id)

    def _job_owned(self, endpoint, job_id):
        self._owned(endpoint)
        if not any(r["payload"].get("provider") == self.provider and r["refs"].get("endpoint") == endpoint
                   and r["payload"].get("job_id") == job_id for r in ledger.read(kinds={"serverless.submit"})):
            raise ValueError("job not submitted by this Gipper ledger")

    def cancel(self, endpoint, job_id, timeout=30):
        self._job_owned(endpoint, job_id)
        try:
            self.backend.cancel(endpoint, job_id)
        except Exception as e:
            self._receipt("cancel_pending", {"job_id": job_id, "error_type": type(e).__name__}, endpoint, job_id)
            raise CleanupPending("cancellation request failed; inspect or delete endpoint before replacement") from e
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self.backend.status(endpoint, job_id)
            except Exception as e:
                self._receipt("cancel_pending", {"job_id": job_id, "error_type": type(e).__name__}, endpoint, job_id)
                raise CleanupPending("cancellation status unavailable; inspect or delete endpoint before replacement") from e
            if result.get("status") in TERMINAL:
                self._receipt("cancel", {"job_id": job_id, "terminal_status": result["status"]}, endpoint, job_id)
                return result
            if time.monotonic() >= deadline:
                self._receipt("cancel_pending", {"job_id": job_id}, endpoint, job_id)
                raise CleanupPending("cancellation unconfirmed; do not submit a replacement")
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def wait(self, endpoint, job_id, timeout=600, poll_seconds=2):
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("timeout and poll_seconds must be finite and positive")
        self._job_owned(endpoint, job_id)
        began = time.monotonic()
        try:
            while True:
                result = self.backend.status(endpoint, job_id)
                if result.get("status") in TERMINAL:
                    self._receipt("result", {k: result[k] for k in
                        ("id", "status", "delayTime", "executionTime", "workerId") if k in result}, endpoint, job_id)
                    return {**result, "client_wait_seconds": round(time.monotonic() - began, 3)}
                remaining = timeout - (time.monotonic() - began)
                if remaining <= 0:
                    raise TimeoutError(f"{endpoint}/{job_id}: wait deadline exceeded")
                time.sleep(min(poll_seconds, remaining))
        except BaseException:
            self.cancel(endpoint, job_id)
            raise

    def run(self, spec, payload):
        """Temporary endpoint, one job, verified teardown even on failure/interruption."""
        began = time.monotonic()
        endpoint = self.create(spec, temporary=True)
        try:
            job_id = self.submit(endpoint, payload, spec.execution_seconds)
            result = self.wait(endpoint, job_id, max(0.001, spec.session_seconds - (time.monotonic() - began)))
            return {**result, "endpoint": endpoint, "provider": self.provider,
                    "client_seconds": round(time.monotonic() - began, 3)}
        finally:
            self.delete(endpoint)
