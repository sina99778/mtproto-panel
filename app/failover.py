"""Anti-filter endpoint engine: health checks, rotation, DNS failover.

What it CAN do from the server side:
  * detect a DOWN endpoint (TCP unreachable) and auto-rotate to a healthy one
  * keep a managed domain's A record pointed at the active endpoint (Cloudflare DNS)
  * one-click manual rotation

What it CANNOT do from the server side: directly know an IP is "filtered inside
Iran" (the panel isn't in Iran). For that, `iran_test()` queries check-host.net's
Iranian probe nodes on demand; auto-rotation only reacts to DOWN endpoints.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

from . import cloudflare, config, database


def is_ip(addr: str) -> bool:
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


def check_tcp(host: str, port: int, timeout: float = 4.0) -> bool:
    """True if we can open a TCP connection to host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _cf_creds():
    token = database.get_setting("cf_api_token")
    zone = database.get_setting("cf_zone_id")
    return (token, zone) if (token and zone) else (None, None)


def sync_dns(proxy, endpoint):
    """If the proxy uses a managed domain and the active endpoint is an IP,
    point the domain's A record at it (best effort)."""
    domain = proxy.get("link_domain")
    if not domain or not endpoint or not is_ip(endpoint["address"]):
        return False, "no-domain-or-not-ip"
    token, zone = _cf_creds()
    if not token:
        return False, "no-cloudflare-credentials"
    return cloudflare.set_dns_a(token, zone, domain, endpoint["address"])


def rotate(proxy_id, prefer_id=None):
    """Promote the best healthy endpoint (or `prefer_id`) to active and sync DNS.
    Returns the new active endpoint dict or None."""
    proxy = database.get_proxy(proxy_id)
    if not proxy:
        return None
    eps = database.list_endpoints(proxy_id)
    if not eps:
        return None
    target = None
    if prefer_id:
        target = next((e for e in eps if e["id"] == prefer_id), None)
    if target is None:
        healthy = [e for e in eps if e["status"] != "down"]
        pool = healthy or eps  # if all down, still pick something
        target = sorted(pool, key=lambda e: (e["priority"], e["id"]))[0]
    database.set_active_endpoint(proxy_id, target["id"])
    target["active"] = 1
    sync_dns(proxy, target)
    return target


def health_check_all(checker=check_tcp):
    """Update every endpoint's status (up/down). Returns counts."""
    now = int(time.time())
    up = down = 0
    for e in database.all_endpoints_for_checks():
        if not e.get("proxy_enabled"):
            continue
        ok = checker(e["address"], e["port"])
        database.update_endpoint_status(e["id"], "up" if ok else "down", now)
        up += 1 if ok else 0
        down += 0 if ok else 1
    return {"up": up, "down": down}


def auto_failover():
    """For proxies with auto_rotate on: if the active endpoint is down (or none),
    rotate to a healthy one. Returns list of (proxy_id, new_address)."""
    rotated = []
    for p in database.list_proxies():
        if not p.get("auto_rotate"):
            continue
        eps = database.list_endpoints(p["id"])
        if not eps:
            continue
        active = next((e for e in eps if e["active"]), None)
        healthy = [e for e in eps if e["status"] == "up"]
        if (active is None or active["status"] == "down") and healthy:
            tgt = rotate(p["id"])
            if tgt:
                rotated.append((p["id"], tgt["address"]))
    return rotated


def iran_test(address, port, max_nodes=12, wait=6.0):
    """On-demand reachability test from check-host.net Iranian nodes.
    Returns {ok, reachable, total, error}. Best effort; blocking ~`wait` seconds."""
    import httpx

    try:
        h = {"Accept": "application/json"}
        r = httpx.get(
            "https://check-host.net/check-tcp",
            params={"host": f"{address}:{int(port)}", "max_nodes": max_nodes},
            headers=h, timeout=20,
        )
        data = r.json()
        rid = data.get("request_id")
        nodes = data.get("nodes") or {}
        if not rid:
            return {"ok": False, "reachable": 0, "total": 0, "error": "no request id"}
        iran_nodes = [n for n in nodes if n.split(".")[0].startswith("ir")]
        time.sleep(wait)
        rr = httpx.get(f"https://check-host.net/check-result/{rid}", headers=h, timeout=20)
        res = rr.json()
        total = reachable = 0
        targets = iran_nodes or list(nodes.keys())
        for n in targets:
            val = res.get(n)
            if val is None:
                continue
            total += 1
            # success looks like [{"address":..,"time":..}]; failure like [{"error":..}] or null
            item = val[0] if isinstance(val, list) and val else None
            if isinstance(item, dict) and "time" in item and "error" not in item:
                reachable += 1
        return {"ok": True, "reachable": reachable, "total": total, "error": None}
    except Exception as e:
        return {"ok": False, "reachable": 0, "total": 0, "error": str(e)}


async def failover_loop():
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, health_check_all)
            await loop.run_in_executor(None, auto_failover)
        except Exception:
            pass
        await asyncio.sleep(config.FAILOVER_INTERVAL)
