"""A small Runpod client: GraphQL for prices and the account, REST for pods and
network volumes. stdlib only; the key only travels in the Authorization header.
"""
import json
import time
import urllib.error
import urllib.request

from gipper.config import redact, runpod_key

GRAPHQL = "https://api.runpod.io/graphql"
REST = "https://rest.runpod.io/v1"


class RunpodError(RuntimeError):
    pass


class Api:
    def __init__(self, key=None, timeout=60):
        self._key = key or runpod_key()
        self.timeout = timeout

    def _req(self, method, url, body=None, retries=3):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                # Cloudflare in front of the API rejects Python's default agent (error 1010)
                "User-Agent": "gipper/0.1 (+https://github.com/lavallee/gipper)", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                msg = redact(e.read().decode(errors="replace")[:500])
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RunpodError(f"{method} {url.split('?')[0]}: HTTP {e.code}: {msg}") from None
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RunpodError(f"{method} {url}: {e.reason}") from None

    def _gql(self, query):
        d = self._req("POST", GRAPHQL, {"query": query})
        if d.get("errors"):
            raise RunpodError(redact(json.dumps(d["errors"])[:500]))
        return d["data"]

    # ------------------------------------------------------------------ account and prices
    def account(self):
        return self._gql("query { myself { clientBalance currentSpendPerHr spendLimit } }")["myself"]

    def gpu_types(self):
        q = ("query { gpuTypes { id displayName memoryInGb secureCloud communityCloud securePrice communityPrice "
             "secureSpotPrice communitySpotPrice lowestPrice(input:{gpuCount:1}){ uninterruptablePrice minimumBidPrice stockStatus } } }")
        return self._gql(q)["gpuTypes"]

    # ------------------------------------------------------------------ pods
    def create_pod(self, spec):
        return self._req("POST", f"{REST}/pods", spec)

    def get_pod(self, pod_id):
        return self._req("GET", f"{REST}/pods/{pod_id}")

    def list_pods(self):
        d = self._req("GET", f"{REST}/pods")
        return d if isinstance(d, list) else d.get("pods", d.get("data", []))

    def terminate_pod(self, pod_id):
        return self._req("DELETE", f"{REST}/pods/{pod_id}")

    # ------------------------------------------------------------------ network volumes
    def volumes(self):
        d = self._req("GET", f"{REST}/networkvolumes")
        return d if isinstance(d, list) else d.get("networkVolumes", d.get("data", []))

    def create_volume(self, name, size_gb, datacenter):
        return self._req("POST", f"{REST}/networkvolumes", {"name": name, "size": size_gb, "dataCenterId": datacenter})

    def delete_volume(self, volume_id):
        return self._req("DELETE", f"{REST}/networkvolumes/{volume_id}")
