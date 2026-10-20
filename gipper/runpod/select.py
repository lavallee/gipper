"""Which GPU: the types that fit, priced, cheapest available first (or the job's
own preference order)."""


def price(g, cloud="COMMUNITY", spot=False):
    if spot:
        return g.get("communitySpotPrice" if cloud == "COMMUNITY" else "secureSpotPrice")
    p = g.get("communityPrice" if cloud == "COMMUNITY" else "securePrice")
    return p or None


def candidates(gpu_types, min_mem_gb, cloud="COMMUNITY", prefer=()):
    """[(gpu_type, $/h, stock)] that fit, preferred first, then cheapest; types Runpod
    reports no stock or price for are dropped."""
    out = []
    for g in gpu_types:
        if (g.get("memoryInGb") or 0) < min_mem_gb:
            continue
        if not g.get("communityCloud" if cloud == "COMMUNITY" else "secureCloud"):
            continue
        p = price(g, cloud)
        if not p:
            continue
        stock = (g.get("lowestPrice") or {}).get("stockStatus")
        out.append((g, p, stock))
    rank = {name: i for i, name in enumerate(prefer)}
    out.sort(key=lambda x: (rank.get(x[0]["id"], len(rank)), x[2] is None, x[1]))
    return out
