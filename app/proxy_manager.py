"""Lifecycle management of proxy instances via systemd.

For every proxy the panel writes a dedicated unit file under
/etc/systemd/system and controls it with systemctl. Two unit shapes:

  * mtproxy-<id>.service  -> runs the mtprotoproxy engine  (direct / cloudflare)
  * mtrelay-<id>.service  -> runs relay.py TCP forwarder    (tunneled)

The panel process must run as root (the systemd unit does, by default).
"""
import shutil
import subprocess
from pathlib import Path

from . import config, engine, utils

PROXY_UNIT_TPL = """[Unit]
Description=MTProto proxy #{id} ({name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
ExecStart={py} {engine} {config_path}
Restart=always
RestartSec=3
LimitNOFILE=1048576
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""

RELAY_UNIT_TPL = """[Unit]
Description=MTProto tunnel relay #{id} ({name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={py} {relay} --listen {port} --upstream-host {uh} --upstream-port {up}
Restart=always
RestartSec=3
LimitNOFILE=1048576
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _proxy_unit_path(pid):
    return config.SYSTEMD_DIR / f"mtproxy-{pid}.service"


def _relay_unit_path(pid):
    return config.SYSTEMD_DIR / f"mtrelay-{pid}.service"


def unit_name(proxy):
    if proxy["mode"] == "tunneled":
        return f"mtrelay-{proxy['id']}.service"
    return f"mtproxy-{proxy['id']}.service"


def _instance_dir(pid):
    d = config.INSTANCES_DIR / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _allow_firewall(port):
    if shutil.which("ufw"):
        _run(["ufw", "allow", f"{int(port)}/tcp"])


def apply(proxy):
    """Write config + unit and (re)start the instance. Idempotent."""
    pid = proxy["id"]
    safe_name = utils.sanitize_oneline(proxy["name"])  # never trust raw text in a unit file
    if proxy["mode"] == "tunneled":
        unit = RELAY_UNIT_TPL.format(
            id=pid, name=safe_name, py=config.VENV_PYTHON, relay=config.RELAY_SCRIPT,
            port=int(proxy["port"]), uh=proxy["upstream_host"], up=int(proxy["upstream_port"]),
        )
        path = _relay_unit_path(pid)
    else:
        d = _instance_dir(pid)
        cfg_path = d / "config.py"
        cfg_path.write_text(
            engine.render_config(proxy["port"], proxy["secret"], proxy["tls_domain"], proxy.get("ad_tag"))
        )
        unit = PROXY_UNIT_TPL.format(
            id=pid, name=safe_name, workdir=str(d),
            py=config.VENV_PYTHON, engine=config.ENGINE_SCRIPT, config_path=str(cfg_path),
        )
        path = _proxy_unit_path(pid)

    path.write_text(unit)
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", path.name])          # start on boot
    _run(["systemctl", "restart", path.name])          # start now / reload after edits
    _allow_firewall(proxy["port"])


def stop(proxy):
    _run(["systemctl", "disable", "--now", unit_name(proxy)])


def restart(proxy):
    _run(["systemctl", "restart", unit_name(proxy)])


def remove(proxy):
    stop(proxy)
    for p in (_proxy_unit_path(proxy["id"]), _relay_unit_path(proxy["id"])):
        if p.exists():
            p.unlink()
    d = config.INSTANCES_DIR / str(proxy["id"])
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    _run(["systemctl", "daemon-reload"])


def status(proxy):
    """Returns 'active', 'inactive', 'failed', or 'unknown'."""
    r = _run(["systemctl", "is-active", unit_name(proxy)])
    out = (r.stdout or "").strip()
    return out if out else "unknown"


def statuses(proxies):
    """Batch variant: one `systemctl is-active` call for all units.
    Returns {proxy_id: state}. `is-active` prints one line per argument, in order."""
    if not proxies:
        return {}
    names = [unit_name(p) for p in proxies]
    ids = [p["id"] for p in proxies]
    r = _run(["systemctl", "is-active", *names])
    lines = (r.stdout or "").splitlines()
    return {ids[i]: (lines[i].strip() if i < len(lines) else "unknown") for i in range(len(ids))}
