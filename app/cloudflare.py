"""Cloudflare Spectrum integration for the "cloudflare" mode.

IMPORTANT (honest limitation): Cloudflare's *free* CDN only proxies
HTTP/WebSocket. MTProto is a raw-TCP protocol that the official Telegram
clients speak directly, so it can NOT traverse the free CDN. To hide the
origin IP behind Cloudflare for MTProto you need **Cloudflare Spectrum**,
which proxies arbitrary TCP. Spectrum is available on paid plans (Pro/Biz/
Enterprise) for generic TCP. This module creates a Spectrum application that
points an edge port (e.g. 443) at your origin proxy port.

If you don't have Spectrum, use the "tunneled" mode with a clean foreign IP
instead — it achieves the same "hide the real server" goal.
"""
import httpx

API = "https://api.cloudflare.com/client/v4"


def create_spectrum_app(token, zone_id, edge_name, edge_port, origin_host, origin_port):
    """Create a Spectrum TCP application. Returns (ok: bool, detail: dict|str)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "protocol": f"tcp/{int(edge_port)}",
        "dns": {"type": "CNAME", "name": edge_name},
        "origin_direct": [f"tcp://{origin_host}:{int(origin_port)}"],
        "tls": "off",
        "ip_firewall": True,
        "proxy_protocol": "off",
        "traffic_type": "direct",
    }
    try:
        r = httpx.post(
            f"{API}/zones/{zone_id}/spectrum/apps", json=body, headers=headers, timeout=30
        )
        data = r.json()
        return bool(data.get("success")), data
    except Exception as e:  # network / parse failure
        return False, str(e)


def delete_spectrum_app(token, zone_id, app_id):
    """Remove a previously created Spectrum app (called when a proxy is deleted)."""
    try:
        r = httpx.delete(
            f"{API}/zones/{zone_id}/spectrum/apps/{app_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        data = r.json()
        return bool(data.get("success")), data
    except Exception as e:
        return False, str(e)


def verify_token(token):
    """Quick credential check. Returns (ok, detail)."""
    try:
        r = httpx.get(
            f"{API}/user/tokens/verify",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = r.json()
        return bool(data.get("success")), data
    except Exception as e:
        return False, str(e)
