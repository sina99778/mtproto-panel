"""Offline operator/ISP detection from a client IP.

Uses a DB-IP "ASN Lite" mmdb database (downloaded by install.sh) read via the
`maxminddb` library. Falls back gracefully to "نامشخص" when the DB or library
is unavailable, so the rest of the stats keep working.

A small curated map turns the major Iranian carrier ASNs into friendly names;
anything else uses the organisation name straight from the database.
"""
from __future__ import annotations

from . import config

# High-confidence Iranian ASNs -> friendly operator names. Anything not here
# falls back to the ASN's organisation string from the database.
IRAN_ASN = {
    197207: "همراه اول (MCI)",
    44244: "ایرانسل",
    57218: "رایتل",
    31549: "شاتل",
    16322: "پارس‌آنلاین",
    12880: "زیرساخت (DCI)",
    58224: "مخابرات ایران (TCI)",
    43754: "آسیاتک",
    50810: "مبین‌نت",
    39501: "پیشگامان",
    25184: "داتک",
    48434: "های‌وب",
    41881: "صبانت",
    207006: "پویا ارتباط",
}

UNKNOWN = "نامشخص"

_reader = None  # None=not tried, False=unavailable, else a maxminddb reader
_cache: dict[str, tuple] = {}


def _get_reader():
    global _reader
    if _reader is False:
        return None
    if _reader is None:
        try:
            import maxminddb
            from pathlib import Path

            if Path(config.ASN_DB_PATH).exists():
                _reader = maxminddb.open_database(config.ASN_DB_PATH)
            else:
                _reader = False
                return None
        except Exception:
            _reader = False
            return None
    return _reader


def lookup(ip: str):
    """Return (operator_name, asn|None). Cached per IP."""
    if ip in _cache:
        return _cache[ip]
    operator, asn = UNKNOWN, None
    reader = _get_reader()
    if reader is not None:
        try:
            rec = reader.get(ip)
            if rec:
                asn = rec.get("autonomous_system_number")
                org = rec.get("autonomous_system_organization") or ""
                operator = IRAN_ASN.get(asn) or org or UNKNOWN
        except Exception:
            pass
    result = (operator, asn)
    _cache[ip] = result
    return result
