"""Central configuration. All values come from environment variables
(seeded by install.sh into data/panel.env) with sane defaults so the
code also runs locally for development."""
import os
from pathlib import Path

# --- Base paths -----------------------------------------------------------
BASE_DIR = Path(os.environ.get("PANEL_BASE_DIR", "/opt/mtproto-panel"))
DATA_DIR = Path(os.environ.get("PANEL_DATA_DIR", str(BASE_DIR / "data")))
INSTANCES_DIR = DATA_DIR / "instances"
DB_PATH = Path(os.environ.get("PANEL_DB_PATH", str(DATA_DIR / "panel.db")))

# --- Runtime binaries / scripts ------------------------------------------
VENV_PYTHON = os.environ.get("PANEL_VENV_PYTHON", str(BASE_DIR / "venv" / "bin" / "python3"))
ENGINE_SCRIPT = os.environ.get(
    "PANEL_ENGINE_SCRIPT", str(BASE_DIR / "engine" / "mtprotoproxy" / "mtprotoproxy.py")
)
RELAY_SCRIPT = os.environ.get("PANEL_RELAY_SCRIPT", str(BASE_DIR / "app" / "relay.py"))
SYSTEMD_DIR = Path(os.environ.get("PANEL_SYSTEMD_DIR", "/etc/systemd/system"))

# --- Web / auth -----------------------------------------------------------
SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "dev-insecure-secret-change-me")
ADMIN_USER = os.environ.get("PANEL_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("PANEL_ADMIN_PASSWORD", "")  # only used to seed first admin
PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8088"))

# --- Statistics / monitoring ---------------------------------------------
STATS_ENABLED = os.environ.get("PANEL_STATS_ENABLED", "1") == "1"
STATS_INTERVAL = int(os.environ.get("PANEL_STATS_INTERVAL", "15"))  # seconds between samples
ASN_DB_PATH = os.environ.get("PANEL_ASN_DB", str(DATA_DIR / "dbip-asn-lite.mmdb"))

# --- Version --------------------------------------------------------------
try:
    VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "dev"
except Exception:
    VERSION = "dev"

# Ensure data dirs exist on import (harmless on every platform).
DATA_DIR.mkdir(parents=True, exist_ok=True)
INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
