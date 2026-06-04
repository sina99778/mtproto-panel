"""Tests for the anti-filter / failover engine:  python tests/test_failover.py

Covers endpoint CRUD, health checks (mocked), auto-failover, manual rotate,
priority selection, and DNS sync (Cloudflare call mocked).
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="mtpanel-fo-"))
os.environ.update(
    PANEL_BASE_DIR=str(TMP), PANEL_DATA_DIR=str(TMP / "data"),
    PANEL_DB_PATH=str(TMP / "data" / "panel.db"),
    PANEL_STATS_ENABLED="0", PANEL_FAILOVER_ENABLED="0",
)

from app import database, failover  # noqa: E402

PASS = 0


def check(label, cond):
    global PASS
    assert cond, f"FAILED: {label}"
    PASS += 1
    print(f"  ok  {label}")


def mkproxy(port):
    return database.create_proxy({
        "name": "p", "mode": "direct", "port": port, "secret": "x" * 32, "tls_domain": "d",
        "ad_tag": None, "upstream_host": None, "upstream_port": None,
        "cf_domain": None, "cf_edge_port": 443, "front_host": None, "enabled": 1,
    })


database.init_db()
pid = mkproxy(8443)

print("[endpoint CRUD]")
e1 = database.add_endpoint(pid, "1.1.1.1", "first", 100)
e2 = database.add_endpoint(pid, "2.2.2.2", "second", 50)
check("first endpoint auto-activates", database.get_active_endpoint(pid)["address"] == "1.1.1.1")
check("list returns ordered by priority", [e["address"] for e in database.list_endpoints(pid)] == ["2.2.2.2", "1.1.1.1"])
check("is_ip works", failover.is_ip("1.1.1.1") and not failover.is_ip("a.example.com"))

print("[health check (mocked)]")
def checker(host, port):
    return host == "2.2.2.2"  # only the second is reachable
failover.health_check_all(checker=checker)
st = {e["address"]: e["status"] for e in database.list_endpoints(pid)}
check("down endpoint flagged down", st["1.1.1.1"] == "down")
check("up endpoint flagged up", st["2.2.2.2"] == "up")

print("[auto-failover]")
database.set_auto_rotate(pid, True)
# active is 1.1.1.1 (down); healthy 2.2.2.2 exists -> should rotate
moved = failover.auto_failover()
check("auto-failover moved off the down endpoint", database.get_active_endpoint(pid)["address"] == "2.2.2.2")
check("auto-failover reported the change", any(a == "2.2.2.2" for _, a in moved))

print("[manual rotate + priority]")
# reset statuses to up so rotate() is free to pick by priority
failover.health_check_all(checker=lambda h, p: True)
failover.rotate(pid)  # no prefer -> lowest priority healthy = 2.2.2.2 (prio 50)
check("rotate picks lowest-priority healthy", database.get_active_endpoint(pid)["address"] == "2.2.2.2")
failover.rotate(pid, prefer_id=e1)
check("rotate honors prefer_id", database.get_active_endpoint(pid)["address"] == "1.1.1.1")

print("[DNS sync]")
ok, info = failover.sync_dns(database.get_proxy(pid), database.get_active_endpoint(pid))
check("no DNS sync without a domain", ok is False)
database.set_link_domain(pid, "proxy.example.com")
database.set_setting("cf_api_token", "tok")
database.set_setting("cf_zone_id", "zone")
called = {}
failover.cloudflare.set_dns_a = lambda t, z, n, ip, **k: (called.update(name=n, ip=ip, token=t) or (True, {}))
failover.rotate(pid, prefer_id=e2)  # active 2.2.2.2 + domain set -> DNS sync fires
check("rotate updates DNS A record to active endpoint", called.get("name") == "proxy.example.com" and called.get("ip") == "2.2.2.2")

print("[iran_test graceful failure]")
res = failover.iran_test("203.0.113.9", 8443, wait=0.0)
check("iran_test returns a well-formed dict", set(["ok", "reachable", "total", "error"]).issubset(res.keys()))

print(f"\n=== {PASS} FAILOVER CHECKS PASSED ===")
