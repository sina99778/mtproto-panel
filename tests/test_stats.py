"""Tests for the statistics collector:  python tests/test_stats.py

Covers the `ss` parser, the cumulative byte-delta logic (prime / growth /
socket-close / counter-reset), session counting, loopback exclusion and the
operator aggregation — all with a mocked `ss` runner and a fake geoip lookup.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="mtpanel-stats-"))
os.environ.update(
    PANEL_BASE_DIR=str(TMP),
    PANEL_DATA_DIR=str(TMP / "data"),
    PANEL_DB_PATH=str(TMP / "data" / "panel.db"),
    PANEL_STATS_ENABLED="0",
)

from app import database, stats  # noqa: E402

PASS = 0


def check(label, cond):
    global PASS
    assert cond, f"FAILED: {label}"
    PASS += 1
    print(f"  ok  {label}")


def build_ss(entries):
    """entries: list of (local_port, peer_ip, peer_port, total_bytes)."""
    lines = []
    for lp, pip, pp, b in entries:
        lines.append(f"ESTAB 0 0 10.0.0.1:{lp} {pip}:{pp}")
        lines.append(f"\t cubic wscale:7,7 rto:204 bytes_sent:{b} bytes_received:0 segs_out:9")
    return "\n".join(lines) + "\n"


FAKE_OP = {"5.6.7.8": "همراه اول", "9.9.9.9": "ایرانسل", "7.7.7.7": "همراه اول"}


def fake_lookup(ip):
    return (FAKE_OP.get(ip, "نامشخص"), 12345)


database.init_db()

# --- 1) parser ------------------------------------------------------------
print("[parser]")
parsed = stats.parse_ss(build_ss([(8443, "5.6.7.8", 111, 3000), (9443, "1.1.1.1", 222, 50)]))
check("groups sockets by local port", set(parsed.keys()) == {8443, 9443})
check("extracts peer ip + bytes", parsed[8443][0] == ("5.6.7.8", 111, 3000))

ipv6 = stats.parse_ss("ESTAB 0 0 10.0.0.1:8443 [2001:db8::1]:5555\n\t cubic bytes_sent:7 bytes_received:3\n")
check("parses IPv6 peer + sums sent/received", ipv6[8443][0] == ("2001:db8::1", 5555, 10))

no_info = stats.parse_ss("ESTAB 0 0 10.0.0.1:8443 5.6.7.8:1\nESTAB 0 0 10.0.0.1:8443 6.6.6.6:2\n")
check("socket without info line flushes with 0 bytes", no_info[8443] == [("5.6.7.8", 1, 0), ("6.6.6.6", 2, 0)])

# --- 2) collect_once: prime / growth / new / close / reset ----------------
print("[collect_once byte-delta logic]")
proxies = [{"id": 1, "port": 8443}, {"id": 2, "port": 9443}]

# cycle 1 (PRIME): nothing should be counted yet
c1 = build_ss([(8443, "5.6.7.8", 111, 3000), (8443, "9.9.9.9", 222, 1000), (9443, "1.1.1.1", 1, 500)])
stats.collect_once(proxies, runner=lambda: c1, lookup=fake_lookup, now=1000)
check("prime cycle counts 0 bytes", database.proxy_stats(1)["cum_bytes"] == 0)
check("prime cycle counts 0 sessions", database.proxy_stats(1)["sessions"] == 0)
check("active is recorded during prime", database.latest_sample(1)["active"] == 2)

# cycle 2: 5.6.7.8 grows +5000, 9.9.9.9 closes, 7.7.7.7 is new (+2000, +1 session)
c2 = build_ss([(8443, "5.6.7.8", 111, 8000), (8443, "7.7.7.7", 333, 2000), (9443, "1.1.1.1", 1, 500)])
stats.collect_once(proxies, runner=lambda: c2, lookup=fake_lookup, now=1015)
check("growth + new socket counted (5000+2000)", database.proxy_stats(1)["cum_bytes"] == 7000)
check("only the new socket is a new session", database.proxy_stats(1)["sessions"] == 1)
check("rate computed from last two samples (7000B/15s)", round(stats._rate_bps(1)) == round(7000 / 15))

# cycle 3: 5.6.7.8 counter RESET to 100 (=> count 100 + new session), 7.7.7.7 unchanged
c3 = build_ss([(8443, "5.6.7.8", 111, 100), (8443, "7.7.7.7", 333, 2000)])
stats.collect_once(proxies, runner=lambda: c3, lookup=fake_lookup, now=1030)
check("counter reset counted as fresh bytes (7000+100)", database.proxy_stats(1)["cum_bytes"] == 7100)
check("counter reset counts a new session (=2)", database.proxy_stats(1)["sessions"] == 2)

# --- 3) operators + loopback exclusion ------------------------------------
print("[operators + loopback]")
ops = database.top_operators(1)
top = {o["operator"]: o["c"] for o in ops}
check("operator aggregation by distinct IP", top.get("همراه اول") == 2 and top.get("ایرانسل") == 1)
check("unique clients on proxy 1 == 3", database.clients_count(1) == 3)

# cycle 4: a loopback peer appears (Paqet-on-kharej case)
c4 = build_ss([(8443, "127.0.0.1", 999, 50), (8443, "7.7.7.7", 333, 2000)])
stats.collect_once(proxies, runner=lambda: c4, lookup=fake_lookup, now=1045)
check("loopback counted as active connection", database.latest_sample(1)["active"] == 2)
check("loopback NOT added to clients/operators", database.clients_count(1) == 3)

# --- 4) snapshot ----------------------------------------------------------
print("[snapshot]")
pid = database.create_proxy({
    "name": "Real", "mode": "direct", "port": 8443, "secret": "x" * 32,
    "tls_domain": "d", "ad_tag": None, "upstream_host": None, "upstream_port": None,
    "cf_domain": None, "cf_edge_port": 443, "front_host": None, "enabled": 1,
})
snap = stats.snapshot()
check("snapshot has totals", "active" in snap["totals"] and "cum_bytes" in snap["totals"])
check("snapshot lists proxies", any(r["id"] == pid for r in snap["proxies"]))

print(f"\n=== {PASS} STATS CHECKS PASSED ===")
