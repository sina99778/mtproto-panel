"""Standalone integration test (no pytest needed):  python tests/test_app.py

Boots the full FastAPI app with a temporary DB + temp systemd dir and a mocked
`systemctl`, then drives the real HTTP flow: login, create proxies (direct /
tunneled / cloudflare), dashboard rendering, toggle, delete, settings, plus
two security checks (systemd-injection via upstream host and via proxy name).
"""
import html
import os
import sys
import tempfile
import types
from pathlib import Path

# --- Sandbox the panel into temp dirs BEFORE importing app modules ---------
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="mtpanel-test-"))
SYSD = TMP / "systemd"
SYSD.mkdir(parents=True, exist_ok=True)

os.environ.update(
    PANEL_BASE_DIR=str(TMP),
    PANEL_DATA_DIR=str(TMP / "data"),
    PANEL_DB_PATH=str(TMP / "data" / "panel.db"),
    PANEL_SYSTEMD_DIR=str(SYSD),
    PANEL_SECRET_KEY="test-secret-key-fixed",
    PANEL_ADMIN_USER="admin",
    PANEL_ADMIN_PASSWORD="initpass123",
    PANEL_STATS_ENABLED="0",
    PANEL_FAILOVER_ENABLED="0",
    PANEL_VENV_PYTHON="/opt/mtproto-panel/venv/bin/python3",
    PANEL_ENGINE_SCRIPT="/opt/mtproto-panel/engine/mtprotoproxy/mtprotoproxy.py",
    PANEL_RELAY_SCRIPT="/opt/mtproto-panel/app/relay.py",
)

from starlette.testclient import TestClient  # noqa: E402

from app import database, main, proxy_manager, utils  # noqa: E402

# --- Mock systemd + public-IP detection -----------------------------------
CALLS = []


def fake_run(cmd):
    CALLS.append(cmd)
    out = ""
    if cmd[:2] == ["systemctl", "is-active"]:
        out = "\n".join("active" for _ in cmd[2:])
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


proxy_manager._run = fake_run
utils.detect_public_ip = lambda: "203.0.113.9"
main.utils.detect_public_ip = lambda: "203.0.113.9"


def units():
    return sorted(p.name for p in SYSD.glob("*.service"))


PASS = 0


def check(label, cond):
    global PASS
    assert cond, f"FAILED: {label}"
    PASS += 1
    print(f"  ok  {label}")


# --- Run -------------------------------------------------------------------
with TestClient(main.app) as c:
    print("[auth]")
    r = c.get("/", follow_redirects=False)
    check("anonymous / redirects to login", r.status_code == 303 and r.headers["location"] == "/login")
    r = c.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
    check("wrong password rejected (401)", r.status_code == 401)
    r = c.post("/login", data={"username": "admin", "password": "initpass123"}, follow_redirects=False)
    check("correct password logs in (303->/)", r.status_code == 303 and r.headers["location"] == "/")
    r = c.get("/")
    check("dashboard renders", r.status_code == 200 and "پروکسی‌های من" in r.text)

    print("[create: direct]")
    r = c.post("/proxies", data={
        "name": "Main", "mode": "direct", "port": "8443",
        "tls_domain": "www.cloudflare.com", "secret": "", "ad_tag": "",
    }, follow_redirects=False)
    check("direct proxy created (303)", r.status_code == 303)
    p = database.list_proxies()[0]
    check("config.py written", (TMP / "data" / "instances" / str(p["id"]) / "config.py").exists())
    check("mtproxy unit written", f"mtproxy-{p['id']}.service" in units())
    r = c.get("/")
    # The browser/JS reads input.value (HTML-entity-decoded), so unescape first.
    page = html.unescape(r.text)
    check("direct link uses server IP + port", "server=203.0.113.9&port=8443&secret=ee" in page)

    print("[create: cloudflare — must use EDGE host:port, not origin]")
    r = c.post("/proxies", data={
        "name": "CF", "mode": "cloudflare", "port": "9443",
        "tls_domain": "www.cloudflare.com", "cf_domain": "proxy.example.com", "cf_edge_port": "443",
    }, follow_redirects=False)
    check("cloudflare proxy created", r.status_code == 303)
    page = html.unescape(c.get("/").text)
    check("cf link uses edge domain:443 (bug #1 fixed)",
          "server=proxy.example.com&port=443&secret=ee" in page)
    check("cf link does NOT leak origin port 9443", "port=9443" not in page)

    print("[create: tunneled]")
    r = c.post("/proxies", data={
        "name": "Tunnel", "mode": "tunneled", "port": "7443",
        "tls_domain": "www.cloudflare.com",
        "secret": "473ce5d4958eb5f968c87680a23854a0",
        "upstream_host": "198.51.100.7", "upstream_port": "443",
    }, follow_redirects=False)
    check("tunneled proxy created", r.status_code == 303)
    tp = [x for x in database.list_proxies() if x["mode"] == "tunneled"][0]
    relay_unit = (SYSD / f"mtrelay-{tp['id']}.service").read_text()
    check("relay unit forwards to upstream", "--upstream-host 198.51.100.7 --upstream-port 443" in relay_unit)

    print("[security: systemd injection via upstream host]")
    before = units()
    r = c.post("/proxies", data={
        "name": "evil", "mode": "tunneled", "port": "6001",
        "upstream_host": "1.2.3.4\nExecStart=/bin/touch /tmp/pwned",
        "upstream_port": "443",
    }, follow_redirects=False)
    check("malicious upstream rejected (400)", r.status_code == 400)
    check("no new unit created from injection", units() == before)

    print("[security: control chars in name are sanitized in unit file]")
    r = c.post("/proxies", data={
        "name": "x\n[Service]\nExecStart=/bin/sh -c pwned", "mode": "direct", "port": "6002",
    }, follow_redirects=False)
    check("proxy with nasty name created", r.status_code == 303)
    np = [x for x in database.list_proxies() if x["port"] == 6002][0]
    unit_txt = (SYSD / f"mtproxy-{np['id']}.service").read_text()
    exec_lines = [ln for ln in unit_txt.splitlines() if ln.strip().startswith("ExecStart=")]
    check("exactly one ExecStart *directive line* in unit", len(exec_lines) == 1)
    check("injected sh is neutralized (no ExecStart=/bin/sh directive)",
          not any(ln.strip().startswith("ExecStart=/bin/sh") for ln in unit_txt.splitlines()))
    desc_lines = [ln for ln in unit_txt.splitlines() if ln.startswith("Description=")]
    check("nasty name confined to a single Description line", len(desc_lines) == 1)

    print("[front_host: tunnel/Paqet topology shows Iran IP in link]")
    r = c.post("/proxies", data={
        "name": "Fronted", "mode": "direct", "port": "5443",
        "tls_domain": "www.cloudflare.com", "secret": "473ce5d4958eb5f968c87680a23854a0",
        "front_host": "5.6.7.8",  # Iran server that fronts this proxy via a tunnel
    }, follow_redirects=False)
    check("fronted proxy created", r.status_code == 303)
    page = html.unescape(c.get("/").text)
    check("link uses front_host (Iran IP), not origin IP",
          "server=5.6.7.8&port=5443&secret=ee473ce5d4958eb5f968c87680a23854a0" in page)
    check("origin IP 203.0.113.9 not used for fronted proxy's 5443 link",
          "server=203.0.113.9&port=5443" not in page)
    r = c.post("/proxies", data={"name": "bad", "mode": "direct", "port": "5444",
                                 "front_host": "1.2.3.4 evil"}, follow_redirects=False)
    check("invalid front_host rejected (400)", r.status_code == 400)

    print("[antifilter: endpoints + link precedence + rotate]")
    r = c.post("/proxies", data={"name": "AF", "mode": "direct", "port": "8499",
                                 "secret": "473ce5d4958eb5f968c87680a23854a0"}, follow_redirects=False)
    afp = [x for x in database.list_proxies() if x["port"] == 8499][0]
    r = c.get("/antifilter")
    check("antifilter page renders", r.status_code == 200 and "Endpoint" in r.text)
    c.post(f"/antifilter/{afp['id']}/endpoints", data={"address": "9.8.7.6", "label": "ir1", "priority": "10"},
           follow_redirects=False)
    check("endpoint added + auto-active", len(database.list_endpoints(afp["id"])) == 1)
    page = html.unescape(c.get("/").text)
    check("link uses active endpoint IP", "server=9.8.7.6&port=8499&secret=ee" in page)
    c.post(f"/antifilter/{afp['id']}/endpoints", data={"address": "5.4.3.2", "label": "ir2", "priority": "5"})
    e2 = [e for e in database.list_endpoints(afp["id"]) if e["address"] == "5.4.3.2"][0]
    c.post(f"/antifilter/endpoints/{e2['id']}/activate", follow_redirects=False)
    check("activate switches active endpoint", database.get_active_endpoint(afp["id"])["address"] == "5.4.3.2")
    page = html.unescape(c.get("/").text)
    check("link follows the rotated endpoint", "server=5.4.3.2&port=8499" in page)
    c.post(f"/antifilter/endpoints/{e2['id']}/delete", follow_redirects=False)
    check("endpoint delete works", all(e["address"] != "5.4.3.2" for e in database.list_endpoints(afp["id"])))

    print("[validation: bad port -> friendly 400, not 422]")
    r = c.post("/proxies", data={"name": "bad", "mode": "direct", "port": "99999"}, follow_redirects=False)
    check("out-of-range port rejected with 400", r.status_code == 400)
    r = c.post("/proxies", data={"name": "bad", "mode": "direct", "port": "abc"}, follow_redirects=False)
    check("non-numeric port rejected with 400", r.status_code == 400)

    print("[toggle + delete]")
    r = c.post(f"/proxies/{p['id']}/toggle", follow_redirects=False)
    check("toggle off persists enabled=0", database.get_proxy(p["id"])["enabled"] == 0)
    r = c.post(f"/proxies/{p['id']}/toggle", follow_redirects=False)
    check("toggle on persists enabled=1", database.get_proxy(p["id"])["enabled"] == 1)
    r = c.post(f"/proxies/{p['id']}/delete", follow_redirects=False)
    check("delete removes row", database.get_proxy(p["id"]) is None)
    check("delete removes unit file", f"mtproxy-{p['id']}.service" not in units())

    print("[stats page + api]")
    r = c.get("/stats")
    check("stats page renders", r.status_code == 200 and "آمار و مانیتورینگ" in r.text)
    r = c.get("/api/stats")
    check("stats api returns json with totals", r.status_code == 200 and "totals" in r.json())
    r = c.get("/api/series")
    check("series api returns points array", r.status_code == 200 and "points" in r.json())

    print("[settings + password change]")
    r = c.post("/settings", data={"server_public_ip": "203.0.113.9", "cf_api_token": "",
                                   "cf_zone_id": "", "new_password": "abc", "new_password2": "xyz"})
    check("mismatched passwords show error", "یکسان نیستند" in r.text)
    r = c.post("/settings", data={"server_public_ip": "203.0.113.9", "cf_api_token": "",
                                  "cf_zone_id": "", "new_password": "newpass123", "new_password2": "newpass123"})
    check("password change accepted", r.status_code == 200)
    c.get("/logout")
    r = c.post("/login", data={"username": "admin", "password": "newpass123"}, follow_redirects=False)
    check("re-login with new password works", r.status_code == 303)

print(f"\n=== {PASS} CHECKS PASSED — integration OK ===")
