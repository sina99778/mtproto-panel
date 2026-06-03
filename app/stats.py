"""Connection / traffic / operator statistics collector.

Engine-agnostic: it reads live data from the OS (`ss`) rather than from the
proxy engine, so it works for direct, tunneled and cloudflare modes alike.

  * active connections + client IPs   -> parsed from `ss`
  * cumulative traffic (bytes)         -> per-socket bytes_sent/received deltas
  * number of uses (sessions)          -> new sockets per cycle
  * operator distribution              -> geoip.lookup() on public client IPs

A single `ss` call per cycle lists all established TCP sockets; we bucket them
by local port and map ports back to proxies.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import subprocess
import time

from . import config, database, geoip

# Per-proxy sampler state: {proxy_id: {"seen": {(ip,port): bytes}, "primed": bool}}
_state: dict[int, dict] = {}

_BYTES_SENT = re.compile(r"bytes_sent:(\d+)")
_BYTES_RECV = re.compile(r"bytes_received:(\d+)")


# --- parsing --------------------------------------------------------------
def _parse_socket_line(line: str):
    """Return (local_port, peer_ip, peer_port) or None."""
    parts = line.split()
    if len(parts) < 2:
        return None
    local, peer = parts[-2], parts[-1]
    try:
        local_port = int(local.rsplit(":", 1)[-1])
        peer_ip = peer.rsplit(":", 1)[0].strip("[]")
        peer_port = int(peer.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return None
    return local_port, peer_ip, peer_port


def _extract_bytes(line: str) -> int:
    s = _BYTES_SENT.search(line)
    r = _BYTES_RECV.search(line)
    return (int(s.group(1)) if s else 0) + (int(r.group(1)) if r else 0)


def parse_ss(text: str) -> dict:
    """Parse `ss -Htin` output -> {local_port: [(peer_ip, peer_port, bytes), ...]}."""
    result: dict[int, list] = {}
    pending = None  # socket awaiting its info line

    def flush(sock, byte_count):
        lp, pip, pp = sock
        result.setdefault(lp, []).append((pip, pp, byte_count))

    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0].isspace():  # info (tcp_info) line belongs to the previous socket
            if pending is not None:
                flush(pending, _extract_bytes(line))
                pending = None
        else:
            if pending is not None:  # previous socket had no info line
                flush(pending, 0)
            pending = _parse_socket_line(line)
    if pending is not None:
        flush(pending, 0)
    return result


def _is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified
    except ValueError:
        return True


def _run_ss() -> str:
    try:
        return subprocess.run(
            ["ss", "-Htin", "state", "established"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


# --- collection -----------------------------------------------------------
def collect_once(proxies, runner=None, lookup=None, now=None):
    """Run one sampling cycle. Returns {proxy_id: {...}} (also persists to DB)."""
    runner = runner or _run_ss
    lookup = lookup or geoip.lookup
    now = int(now if now is not None else time.time())

    by_port = parse_ss(runner())
    out = {}

    for p in proxies:
        pid, port = p["id"], p["port"]
        st = _state.setdefault(pid, {"seen": {}, "primed": False})
        primed = st["primed"]
        seen = st["seen"]

        current = {}
        delta = 0
        sessions_new = 0
        client_hits = []  # (ip, is_new_session)
        active = 0

        for pip, pport, b in by_port.get(port, []):
            active += 1
            key = (pip, pport)
            current[key] = b
            prev = seen.get(key)
            is_new = primed and prev is None
            if primed:
                if prev is None:
                    delta += b
                    sessions_new += 1
                elif b >= prev:
                    delta += b - prev
                else:  # tuple reused or counter reset
                    delta += b
                    sessions_new += 1
            if not _is_private(pip):
                client_hits.append((pip, is_new))

        st["seen"] = current
        st["primed"] = True

        cum_bytes, sessions_total = database.add_proxy_stats(pid, delta, sessions_new, now)
        database.insert_sample(pid, now, active, cum_bytes)
        for ip, is_new in client_hits:
            op, asn = lookup(ip)
            database.upsert_client(pid, ip, op, asn, 1 if is_new else 0, now)

        out[pid] = {"active": active, "cum_bytes": cum_bytes, "sessions_new": sessions_new}

    database.prune_samples()
    return out


def forget(pid):
    _state.pop(pid, None)


# --- reporting ------------------------------------------------------------
def _rate_bps(pid):
    samples = database.two_recent_samples(pid)
    if len(samples) < 2:
        return 0.0
    newest, prev = samples[0], samples[1]
    dt = (newest["ts"] - prev["ts"]) or 1
    return max(0.0, (newest["cum_bytes"] - prev["cum_bytes"]) / dt)


def snapshot():
    proxies = database.list_proxies()
    rows = []
    tot_active = tot_bytes = tot_sessions = 0
    for p in proxies:
        ps = database.proxy_stats(p["id"]) or {"cum_bytes": 0, "sessions": 0}
        ls = database.latest_sample(p["id"])
        active = ls["active"] if ls else 0
        rows.append({
            "id": p["id"], "name": p["name"], "mode": p["mode"],
            "active": active, "cum_bytes": ps["cum_bytes"], "sessions": ps["sessions"],
            "rate_bps": _rate_bps(p["id"]), "clients": database.clients_count(p["id"]),
            "operators": database.top_operators(p["id"]),
        })
        tot_active += active
        tot_bytes += ps["cum_bytes"]
        tot_sessions += ps["sessions"]
    totals = {
        "active": tot_active, "cum_bytes": tot_bytes, "sessions": tot_sessions,
        "clients": database.clients_count(), "operators": database.top_operators(),
    }
    return {"proxies": rows, "totals": totals}


# --- background loop ------------------------------------------------------
async def sampler_loop():
    loop = asyncio.get_running_loop()
    while True:
        try:
            # Run the blocking `ss` + DB work off the event loop so the web UI
            # stays responsive even with many thousands of connections.
            await loop.run_in_executor(None, collect_once, database.list_enabled_proxies())
        except Exception:
            pass
        await asyncio.sleep(config.STATS_INTERVAL)
