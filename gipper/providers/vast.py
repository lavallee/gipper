"""Vast.ai: a marketplace of individual machines (many consumer GPUs). Each offer
is one machine ("ask"); renting it makes a Docker container with direct ssh as
root. Ports, UDP included, are published with docker `-p` options and land on a
random external port of the host's (usually shared) public IP; gipper hands the
mapping to the job as GIPPER_PORT_<n>_<PROTO>.

API: https://docs.vast.ai/api-reference (search is public; renting needs the key).
"""
import time

from gipper.config import settings
from gipper.http import ProviderError, request
from gipper.market import Offer
from gipper.providers import BadHost, Provider, Unavailable
from gipper.ssh import Remote

BASE = "https://console.vast.ai/api"
MIN_RELIABILITY = 0.97   # Vast's own uptime score for the machine
MIN_INET_DOWN = 100      # Mb/s: inputs and model weights have to get there
PER_GPU = 8              # machines per GPU model: enough variety in speed and track record
DEAD = ("exited", "unknown", "offline")   # never reaches running from these (Vast's docs)


def _port_opts(ports):
    """["8082/udp", "8080"] -> {"-p 8082:8082/udp": "1", "-p 8080:8080": "1"}"""
    out = {}
    for p in ports:
        num, _, proto = p.partition("/")
        proto = (proto or "tcp").lower()
        out[f"-p {num}:{num}" + ("/udp" if proto == "udp" else "")] = "1"
    return out


def offers_from(rows, per_gpu=PER_GPU):
    """Offers from Vast search rows. Memory is decimal GB from Vast's MiB (an A40's
    46068 MiB is 48.3 GB, a 4090's 24564 MiB is 25.8 GB), so nominal sizes qualify.
    Speeds are Mb/s and fees $/GB, as Vast advertises them."""
    out, per = [], {}
    for o in rows:
        name = o.get("gpu_name") or "?"
        if per.get(name, 0) >= per_gpu:
            continue
        per[name] = per.get(name, 0) + 1
        geo = o.get("geolocation") or ""
        cap = o.get("compute_cap")
        out.append(Offer("vast", name, (o.get("gpu_ram") or 0) * 1024**2 / 1e9, float(o["dph_total"]), where=geo,
                         country=geo.rsplit(",", 1)[-1].strip().upper() or None, stock="yes", udp=True,
                         ref={"ask": o["id"], "machine": str(o["machine_id"]) if o.get("machine_id") else None},
                         metrics={"host_download_mbps": o.get("inet_down"), "host_upload_mbps": o.get("inet_up"),
                                  "host_download_usd_per_gb": o.get("inet_down_cost"),
                                  "host_upload_usd_per_gb": o.get("inet_up_cost"),
                                  "cuda_max_good": o.get("cuda_max_good"),
                                  "compute_capability": cap / 100 if cap and cap >= 100 else cap,
                                  "reliability": o.get("reliability")}))
    return out


class Vast(Provider):
    name = "vast"
    key_name = "VAST_API_KEY"
    udp = True

    def _req(self, method, path, body=None, params=None, auth=True):
        headers = {"Authorization": f"Bearer {self.key()}"} if auth else {}
        return request(method, f"{BASE}{path}", body, headers=headers, params=params)

    def offers(self, job):
        p = job.placement
        q = {"verified": {"eq": True}, "external": {"eq": False}, "rentable": {"eq": True}, "rented": {"eq": False},
             "num_gpus": {"eq": 1}, "gpu_ram": {"gte": job.min_mem_gb * 1e9 / 1024**2},   # MiB in the API
             "reliability": {"gte": MIN_RELIABILITY},
             "inet_down": {"gte": max(MIN_INET_DOWN, p.min_host_download_mbps or 0)},
             "direct_port_count": {"gte": 1 + len(job.ports)},   # ssh plus the job's own
             "type": "ondemand", "order": [["dph_total", "asc"]], "limit": 200,
             "allocated_storage": job.container_disk_gb}   # so dph_total includes the disk
        for key, floor in (("inet_up", p.min_host_upload_mbps), ("cuda_max_good", p.min_cuda),
                           ("compute_cap", p.min_compute_capability * 100 if p.min_compute_capability else None)):
            if floor:
                q[key] = {"gte": floor}
        if job.regions:
            q["geolocation"] = {"in": [r.upper() for r in job.regions]}
        return offers_from(self._req("POST", "/v0/bundles/", q, auth=self.has_key()).get("offers", []))

    def _ensure_key(self, pubkey):
        """Vast adds account keys to new instances; register gipper's once."""
        d = self._req("GET", "/v0/ssh/")
        keys = d if isinstance(d, list) else d.get("ssh_keys", d.get("keys", []))
        body = pubkey.split()[1]
        if not any(body in (k.get("public_key") or k.get("ssh_key") or "") for k in keys if isinstance(k, dict)):
            self._req("POST", "/v0/ssh/", {"ssh_key": pubkey})

    def create(self, offer, job, pubkey, name):
        self._ensure_key(pubkey)
        spec = {"client_id": "me", "image": job.image or settings()["vast_image"], "disk": job.container_disk_gb,
                "runtype": "ssh_direct", "label": name, "env": _port_opts(job.ports), "cancel_unavail": True}
        try:
            d = self._req("PUT", f"/v0/asks/{offer.ref['ask']}/", spec)
        except ProviderError as e:
            if any(w in str(e).lower() for w in ("no_such_ask", "not available", "unavailable", "already rented", "http 404")):
                raise Unavailable(str(e)) from None
            raise
        if not d.get("success") or not d.get("new_contract"):
            raise Unavailable(f"vast would not rent ask {offer.ref['ask']}: {d.get('msg') or d.get('error') or d}")
        return {"id": str(d["new_contract"]), "usd_h": offer.usd_h, "gpu": offer.gpu}

    def get(self, iid):
        d = self._req("GET", f"/v0/instances/{iid}/")
        inst = d.get("instances", d)
        return inst[0] if isinstance(inst, list) else (inst or {})

    def wait_ready(self, iid, job, timeout_s=900):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            i = self.get(iid)
            status = i.get("actual_status")
            if status in DEAD:
                raise BadHost(f"instance {iid} is {status}: {i.get('status_msg') or ''}".strip())
            mapped = i.get("ports") or {}
            ip = i.get("public_ipaddr")
            ssh = mapped.get("22/tcp") if isinstance(mapped, dict) else None
            if status == "running" and ip and ssh:
                ports = {k: {"host": ip, "port": int(v[0]["HostPort"])}
                         for k, v in mapped.items() if k != "22/tcp" and v}
                return Remote(ip.strip(), int(ssh[0]["HostPort"])), {
                    "machine": i.get("machine_id"), "gpu": i.get("gpu_name"), "ports": ports,
                    "where": i.get("geolocation")}
            time.sleep(5)
        raise BadHost(f"instance {iid} had no ssh endpoint after {timeout_s}s")

    def terminate(self, iid):
        self._req("DELETE", f"/v0/instances/{iid}/", {})

    def list_ours(self):
        out, params = [], {"select_filters": {}, "order_by": [{"col": "id", "dir": "asc"}], "limit": 25}
        while True:
            d = self._req("GET", "/v1/instances/", params=params)
            for i in d.get("instances") or []:
                if (i.get("label") or "").startswith("gipper-"):
                    out.append({"id": str(i["id"]), "name": i["label"], "started": i.get("start_date"),
                                "status": i.get("actual_status"), "usd_h": i.get("dph_total")})
            if not d.get("next_token"):
                return out
            params["after_token"] = d["next_token"]

    def billed(self, since):
        """{instance id: (usd, detail)} Vast charged since `since` (epoch s), from its
        itemised charges: GPU while running, disk from creation, bandwidth by the byte."""
        out, params = {}, {"select_filters": {"day": {"gte": int(since), "lte": int(time.time())}}, "limit": 20}
        while True:
            d = self._req("GET", "/v0/charges/", params=params)
            for r in d.get("results") or []:
                if r.get("type") != "instance" or not str(r.get("source", "")).startswith("instance-"):
                    continue
                iid = r["source"].split("-", 1)[1]
                usd, detail = out.get(iid, (0.0, {}))
                for item in r.get("items") or []:
                    detail[item["type"]] = round(detail.get(item["type"], 0) + item.get("amount", 0), 6)
                out[iid] = (usd + r.get("amount", 0), detail)
            if not d.get("next_token"):
                return out
            params["after_token"] = d["next_token"]

    def account(self):
        u = self._req("GET", "/v0/users/current/")
        return f"credit ${float(u.get('credit') or 0):.2f}; balance ${float(u.get('balance') or 0):.2f}"


PROVIDER = Vast()
