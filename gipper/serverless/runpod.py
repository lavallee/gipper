"""Runpod REST v2 control plane and api.runpod.ai queue protocol; stdlib only."""
import re
from urllib.parse import urlencode

from gipper.config import runpod_key
from gipper.http import ProviderError, request

CONTROL = "https://api.runpod.io/v2"
QUEUE = "https://api.runpod.ai/v2"


def validate_options(options):
    allowed = {"pools", "excluded_types", "min_cuda_version", "data_centers", "global_volume", "network_volumes", "flashboot"}
    if set(options) - allowed:
        raise ValueError(f"unknown runpod options: {sorted(set(options) - allowed)}")
    for key in ("pools", "excluded_types", "data_centers", "network_volumes"):
        value = options.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
            raise ValueError(f"{key} must be a list of nonempty strings")
    if not options.get("pools"):
        raise ValueError("runpod.pools required (catalog pool IDs, e.g. ADA_24)")
    if options.get("global_volume") and options.get("network_volumes"):
        raise ValueError("global and network volumes are mutually exclusive")
    if options.get("flashboot", "FLASHBOOT") not in ("OFF", "FLASHBOOT", "PRIORITY_FLASHBOOT"):
        raise ValueError("invalid flashboot mode")
    if not re.fullmatch(r"\d+\.\d+", options.get("min_cuda_version", "12.8")):
        raise ValueError("min_cuda_version must be major.minor")


def _id(value):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise ValueError("invalid provider ID")
    return value


def body(spec):
    validate_options(spec.options)
    o = spec.options
    result = {"name": spec.name, "image": spec.image, "type": "QUEUE", "disk": spec.disk_gb,
              "gpu": {"pools": o["pools"], "excludedTypes": o.get("excluded_types", []), "count": 1,
                      "minCudaVersion": o.get("min_cuda_version", "12.8")},
              "workers": {"min": 0, "max": spec.max_workers, "idleTimeout": spec.idle_seconds},
              "scaling": {"type": "QUEUE_DELAY", "queueDelay": 1},
              "flashboot": o.get("flashboot", "FLASHBOOT"), "timeout": spec.execution_seconds * 1000,
              "dataCenterIds": o.get("data_centers", [])}
    if spec.command:
        result["entrypoint"] = spec.command
        result["cmd"] = []
    if o.get("global_volume"):
        result["mounts"] = {"global": [{"volumeId": o["global_volume"]}]}
    elif o.get("network_volumes"):
        result["mounts"] = {"network": [{"volumeId": v} for v in o["network_volumes"]]}
    return result


class Adapter:
    def _request(self, method, url, payload=None):
        # Never retry non-idempotent POSTs, including ambiguous transport failures.
        return request(method, url, payload, headers={"Authorization": f"Bearer {runpod_key()}"},
                       retries=1 if method == "POST" else 3, timeout=30)

    def create(self, spec):
        return self._request("POST", f"{CONTROL}/serverless", body(spec))

    def get(self, endpoint):
        try:
            return self._request("GET", f"{CONTROL}/serverless/{_id(endpoint)}")
        except ProviderError as e:
            if e.status == 404:
                return None
            raise

    def list(self):
        out, cursor = [], None
        while True:
            query = "?" + urlencode({"cursor": cursor}) if cursor else ""
            page = self._request("GET", f"{CONTROL}/serverless{query}")
            out.extend(page.get("endpoints", []))
            cursor = (page.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return out

    def delete(self, endpoint):
        return self._request("DELETE", f"{CONTROL}/serverless/{_id(endpoint)}")

    def submit(self, endpoint, payload, execution_ms):
        return self._request("POST", f"{QUEUE}/{_id(endpoint)}/run", {
            "input": payload, "policy": {"executionTimeout": execution_ms}})

    def status(self, endpoint, job_id):
        return self._request("GET", f"{QUEUE}/{_id(endpoint)}/status/{_id(job_id)}")

    def cancel(self, endpoint, job_id):
        return self._request("POST", f"{QUEUE}/{_id(endpoint)}/cancel/{_id(job_id)}")

    def health(self, endpoint):
        return self._request("GET", f"{QUEUE}/{_id(endpoint)}/health")

    @staticmethod
    def verify(resource, spec):
        expected = body(spec)
        if not resource:
            raise RuntimeError("endpoint absent after creation")
        for field in ("name", "image", "type", "timeout", "flashboot", "disk", "scaling"):
            if resource.get(field) != expected[field]:
                raise RuntimeError(f"endpoint read-back mismatch: {field}")
        if spec.command and resource.get("entrypoint") != spec.command:
            raise RuntimeError("endpoint read-back mismatch: entrypoint")
        for field, value in expected["workers"].items():
            if resource.get("workers", {}).get(field) != value:
                raise RuntimeError(f"endpoint read-back mismatch: workers.{field}")
        for field, value in expected["gpu"].items():
            default = [] if field == "excludedTypes" else None
            if resource.get("gpu", {}).get(field, default) != value:
                raise RuntimeError(f"endpoint read-back mismatch: gpu.{field}")
        for field in ("mounts", "dataCenterIds"):
            if expected.get(field) and resource.get(field) != expected[field]:
                raise RuntimeError(f"endpoint read-back mismatch: {field}")
