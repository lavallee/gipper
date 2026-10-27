"""Shadeform: one account and one bill across 30+ clouds (Lambda, Hyperstack,
Massed Compute, Crusoe, ...). Instances are whole VMs with their own public IP,
reached by ssh as `shadeform` (not root); drivers come with the "shade_os" images.
Ports, UDP included, are opened with UFW rules at boot. The underlying cloud may
have its own firewall too, so test a UDP job's ports before relying on them.

Each instance is also created with Shadeform's own auto_delete (the job's deadline
and a spend ceiling), a second guard behind gipper's teardown and reaper.

API: https://docs.shadeform.ai/api-reference (the catalogue is public).
"""
import time
from datetime import datetime, timedelta, timezone

from gipper.http import ProviderError, request
from gipper.market import Offer
from gipper.providers import BadHost, Provider, Unavailable
from gipper.ssh import Remote

BASE = "https://api.shadeform.ai/v1"
ERROR = ("error", "failed", "deleted", "deleting")


def _country(display_name):
    """Availability display names lead with the country: "US, Des Moines" -> "US"."""
    head = (display_name or "").split(",", 1)[0].strip().upper()
    return head if len(head) == 2 else None


def offers_from(types):
    """One offer per GPU, cloud, region and price: a cloud often lists the same card
    under several instance types ("A6000", "A6000_plus", "A6000_base")."""
    out, seen = [], set()
    for t in types or []:
        conf = t.get("configuration") or {}
        if (t.get("num_gpus") or conf.get("num_gpus")) != 1:
            continue
        for a in t.get("availability") or []:
            if not a.get("available") or a.get("rental_type", "on_demand") != "on_demand":
                continue
            key = (t["cloud"], a["region"], t.get("gpu_type"), t["hourly_price"])
            if key in seen:
                continue
            seen.add(key)
            out.append(Offer("shadeform", t.get("gpu_type") or t["shade_instance_type"],
                             float(conf.get("vram_per_gpu_in_gb") or 0), t["hourly_price"] / 100,
                             where=f"{t['cloud']} {a['region']}", country=_country(a.get("display_name")),
                             stock="yes", udp=True,
                             ref={"cloud": t["cloud"], "region": a["region"], "type": t["shade_instance_type"],
                                  "os": next((o for o in conf.get("os_options") or [] if "cuda" in o), None),
                                  "boot_s": (t.get("boot_time") or {}).get("max_boot_in_sec")}))
    return out


def _ufw(ports):
    rules = [{"port": "22", "proto": "tcp"}]
    for p in ports:
        num, _, proto = p.partition("/")
        rules.append({"port": num.replace("-", ":"), "proto": (proto or "tcp").lower()})
    return rules


class Shadeform(Provider):
    name = "shadeform"
    key_name = "SHADEFORM_API_KEY"
    udp = True

    def _req(self, method, path, body=None, params=None, auth=True):
        headers = {"X-API-KEY": self.key()} if auth else {}
        return request(method, f"{BASE}{path}", body, headers=headers, params=params)

    def offers(self, job):
        d = self._req("GET", "/instances/types", params={"num_gpus": 1, "available": "true", "sort": "price"}, auth=False)
        return offers_from(d.get("instance_types"))

    def _key_id(self, pubkey):
        body = pubkey.split()[1]
        for k in self._req("GET", "/sshkeys").get("ssh_keys") or []:
            if body in (k.get("public_key") or ""):
                return k["id"]
        return self._req("POST", "/sshkeys/add", {"name": "gipper", "public_key": pubkey})["id"]

    def create(self, offer, job, pubkey, name):
        deadline = datetime.now(timezone.utc) + timedelta(hours=job.max_hours, minutes=30)
        spec = {"cloud": offer.ref["cloud"], "region": offer.ref["region"], "shade_instance_type": offer.ref["type"],
                "shade_cloud": True, "name": name[:50], "ssh_key_id": self._key_id(pubkey), "tags": ["gipper"],
                "auto_delete": {"date_threshold": deadline.isoformat(timespec="seconds").replace("+00:00", "Z"),
                                "spend_threshold": f"{max(job.max_usd * 1.5, 1):.2f}"}}
        if offer.ref.get("os"):
            spec["os"] = offer.ref["os"]
        if job.ports:
            spec["networking"] = {"ufw_rules": _ufw(job.ports)}
        try:
            d = self._req("POST", "/instances/create", spec)
        except ProviderError as e:
            if any(w in str(e).lower() for w in ("capacity", "not available", "unavailable", "out of stock")):
                raise Unavailable(str(e)) from None
            raise
        return {"id": d["id"], "usd_h": offer.usd_h, "gpu": offer.gpu}

    def wait_ready(self, iid, job, timeout_s=1200):   # VMs boot slower than containers (5-8 min listed)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            i = self._req("GET", f"/instances/{iid}/info")
            status = (i.get("status") or "").lower()
            if status in ERROR or "error" in status:
                raise BadHost(f"instance {iid} is {status}: {i.get('status_details') or ''}".strip())
            if status == "active" and i.get("ip"):
                # a whole VM: the job's ports are its own, unless the cloud forwards them
                fwd = {f["internal_port"]: f["external_port"] for f in i.get("port_forwards") or []}
                ports = {}
                for p in job.ports:
                    num, _, proto = p.partition("/")
                    if num.isdigit():
                        ports[f"{num}/{(proto or 'tcp').lower()}"] = {"host": i["ip"], "port": fwd.get(int(num), int(num))}
                return Remote(i["ip"], i.get("ssh_port") or fwd.get(22, 22), user=i.get("ssh_user") or "shadeform"), {
                    "machine": i.get("cloud_assigned_id"), "gpu": i.get("shade_instance_type"), "ports": ports,
                    "where": f"{i.get('cloud')} {i.get('region')}"}
            time.sleep(10)
        raise BadHost(f"instance {iid} not active after {timeout_s}s")

    def terminate(self, iid):
        self._req("POST", f"/instances/{iid}/delete")

    def list_ours(self):
        out = []
        for i in self._req("GET", "/instances").get("instances") or []:
            if (i.get("name") or "").startswith("gipper-") and i.get("status") not in ("deleting", "deleted"):
                started = i.get("created_at")
                try:
                    started = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() if started else None
                except ValueError:
                    started = None
                out.append({"id": i["id"], "name": i["name"], "started": started, "status": i.get("status"),
                            "usd_h": (i.get("hourly_price") or 0) / 100})
        return out

    def account(self):
        n = len(self.list_ours())
        return f"key works; {n} gipper instance(s) running (the API reports no balance)"


PROVIDER = Shadeform()
