"""Read-only view of the current kernel network tuning so the dashboard
can show whether BBR is active."""
from pathlib import Path


def _read(p):
    try:
        return Path(p).read_text().strip()
    except Exception:
        return "n/a"


def status():
    cc = _read("/proc/sys/net/ipv4/tcp_congestion_control")
    return {
        "congestion_control": cc,
        "available": _read("/proc/sys/net/ipv4/tcp_available_congestion_control"),
        "qdisc": _read("/proc/sys/net/core/default_qdisc"),
        "bbr_active": cc == "bbr",
    }
