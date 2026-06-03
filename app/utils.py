"""Helpers: secret generation, FakeTLS link building, validation,
public-IP detection."""
import ipaddress
import re
import secrets

import httpx

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")


def gen_secret() -> str:
    """16 random bytes -> 32 hex chars (a valid MTProto secret)."""
    return secrets.token_hex(16)


def normalize_secret(raw: str) -> str:
    """Accept either a raw 32-hex secret or a full client secret
    (dd.../ee...) and return the raw 32-hex part."""
    s = (raw or "").strip().lower()
    if s.startswith("ee"):
        s = s[2:34]
    elif s.startswith("dd"):
        s = s[2:34]
    if not re.fullmatch(r"[0-9a-f]{32}", s):
        raise ValueError("secret must be 32 hexadecimal characters")
    return s


def build_client_secret(raw_secret: str, tls_domain: str) -> str:
    """FakeTLS (ee) secret = 'ee' + raw_secret + hex(domain)."""
    return "ee" + raw_secret + tls_domain.encode("utf-8").hex()


def build_links(host: str, port, raw_secret: str, tls_domain: str) -> dict:
    cs = build_client_secret(raw_secret, tls_domain)
    return {
        "client_secret": cs,
        "tme": f"https://t.me/proxy?server={host}&port={port}&secret={cs}",
        "tg": f"tg://proxy?server={host}&port={port}&secret={cs}",
    }


def valid_domain(d: str) -> bool:
    return bool(_DOMAIN_RE.match((d or "").strip()))


def valid_port(p) -> bool:
    try:
        p = int(p)
    except (TypeError, ValueError):
        return False
    return 1 <= p <= 65535


def valid_host(h: str) -> bool:
    """A valid upstream host: an IPv4/IPv6 literal or a DNS hostname.
    Rejects anything containing whitespace/control chars (systemd-injection safe)."""
    h = (h or "").strip()
    if not h or any(c.isspace() for c in h):
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return valid_domain(h)


def sanitize_oneline(text: str, limit: int = 120) -> str:
    """Collapse to a single safe line for use in systemd unit descriptions."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()[:limit]


def human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_rate(bps) -> str:
    return human_bytes(bps) + "/s"


def detect_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = httpx.get(url, timeout=5)
            ip = r.text.strip()
            if re.fullmatch(r"[0-9.]{7,15}", ip):
                return ip
        except Exception:
            continue
    return ""
