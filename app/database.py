"""Thin SQLite data-access layer. Returns plain dicts (not sqlite3.Row)
so the rest of the code can use .get() and dict spreading freely."""
import sqlite3
from contextlib import contextmanager
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT UNIQUE NOT NULL,
    pw_hash   TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS proxies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    mode          TEXT NOT NULL,              -- direct | tunneled | cloudflare
    port          INTEGER NOT NULL,
    secret        TEXT NOT NULL,              -- raw 16-byte hex (32 chars)
    tls_domain    TEXT NOT NULL DEFAULT 'www.cloudflare.com',
    ad_tag        TEXT,                        -- sponsor channel tag (hex) or NULL
    upstream_host TEXT,                        -- tunneled mode only
    upstream_port INTEGER,                     -- tunneled mode only
    cf_domain     TEXT,                        -- cloudflare mode only
    cf_edge_port  INTEGER DEFAULT 443,         -- cloudflare mode only
    cf_app_id     TEXT,                        -- cloudflare Spectrum app id (for cleanup)
    front_host    TEXT,                        -- public address shown to users (e.g. Iran tunnel IP)
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS proxy_stats (
    proxy_id   INTEGER PRIMARY KEY,
    cum_bytes  INTEGER NOT NULL DEFAULT 0,
    sessions   INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS stats_samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_id  INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    active    INTEGER NOT NULL,
    cum_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_proxy_ts ON stats_samples(proxy_id, ts);

CREATE TABLE IF NOT EXISTS clients (
    proxy_id   INTEGER NOT NULL,
    ip         TEXT NOT NULL,
    operator   TEXT,
    asn        INTEGER,
    sessions   INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER,
    last_seen  INTEGER,
    PRIMARY KEY (proxy_id, ip)
);
"""


def _migrate(db):
    """Add columns introduced after the first release (idempotent)."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(proxies)").fetchall()}
    migrations = (
        ("cf_app_id", "ALTER TABLE proxies ADD COLUMN cf_app_id TEXT"),
        ("front_host", "ALTER TABLE proxies ADD COLUMN front_host TEXT"),
    )
    for name, ddl in migrations:
        if name not in cols:
            db.execute(ddl)


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript(SCHEMA)
        _migrate(db)


def _row(r):
    return dict(r) if r is not None else None


# --- Admins ---------------------------------------------------------------
def get_admin(username):
    with get_db() as db:
        return _row(db.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone())


def create_admin(username, pw_hash):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO admins(username, pw_hash) VALUES (?,?)", (username, pw_hash))


def update_admin_password(username, pw_hash):
    with get_db() as db:
        db.execute("UPDATE admins SET pw_hash=? WHERE username=?", (pw_hash, username))


# --- Settings (key/value) -------------------------------------------------
def get_setting(key, default=None):
    with get_db() as db:
        r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- Proxies --------------------------------------------------------------
PROXY_FIELDS = (
    "name", "mode", "port", "secret", "tls_domain", "ad_tag",
    "upstream_host", "upstream_port", "cf_domain", "cf_edge_port", "front_host", "enabled",
)


def list_proxies():
    with get_db() as db:
        return [_row(r) for r in db.execute("SELECT * FROM proxies ORDER BY id DESC").fetchall()]


def get_proxy(pid):
    with get_db() as db:
        return _row(db.execute("SELECT * FROM proxies WHERE id=?", (pid,)).fetchone())


def port_in_use(port, exclude_id=None):
    with get_db() as db:
        if exclude_id:
            r = db.execute("SELECT 1 FROM proxies WHERE port=? AND id<>?", (port, exclude_id)).fetchone()
        else:
            r = db.execute("SELECT 1 FROM proxies WHERE port=?", (port,)).fetchone()
        return r is not None


def create_proxy(data):
    cols = ", ".join(PROXY_FIELDS)
    placeholders = ", ".join("?" for _ in PROXY_FIELDS)
    values = [data.get(f) for f in PROXY_FIELDS]
    with get_db() as db:
        cur = db.execute(f"INSERT INTO proxies ({cols}) VALUES ({placeholders})", values)
        return cur.lastrowid


def set_enabled(pid, enabled):
    with get_db() as db:
        db.execute("UPDATE proxies SET enabled=? WHERE id=?", (1 if enabled else 0, pid))


def set_cf_app_id(pid, app_id):
    with get_db() as db:
        db.execute("UPDATE proxies SET cf_app_id=? WHERE id=?", (app_id, pid))


def delete_proxy(pid):
    with get_db() as db:
        db.execute("DELETE FROM proxies WHERE id=?", (pid,))
        db.execute("DELETE FROM proxy_stats WHERE proxy_id=?", (pid,))
        db.execute("DELETE FROM stats_samples WHERE proxy_id=?", (pid,))
        db.execute("DELETE FROM clients WHERE proxy_id=?", (pid,))


# --- Statistics -----------------------------------------------------------
def list_enabled_proxies():
    with get_db() as db:
        return [_row(r) for r in db.execute("SELECT * FROM proxies WHERE enabled=1 ORDER BY id").fetchall()]


def add_proxy_stats(pid, delta_bytes, sessions_new, ts):
    """Increment cumulative counters; return (cum_bytes, sessions)."""
    with get_db() as db:
        db.execute(
            "INSERT INTO proxy_stats(proxy_id, cum_bytes, sessions, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(proxy_id) DO UPDATE SET "
            "cum_bytes = cum_bytes + excluded.cum_bytes, "
            "sessions  = sessions  + excluded.sessions, "
            "updated_at = excluded.updated_at",
            (pid, max(0, int(delta_bytes)), max(0, int(sessions_new)), int(ts)),
        )
        r = db.execute("SELECT cum_bytes, sessions FROM proxy_stats WHERE proxy_id=?", (pid,)).fetchone()
        return (r["cum_bytes"], r["sessions"])


def insert_sample(pid, ts, active, cum_bytes):
    with get_db() as db:
        db.execute(
            "INSERT INTO stats_samples(proxy_id, ts, active, cum_bytes) VALUES (?,?,?,?)",
            (pid, int(ts), int(active), int(cum_bytes)),
        )


def prune_samples(keep_seconds=86400):
    with get_db() as db:
        db.execute(
            "DELETE FROM stats_samples WHERE ts < "
            "(SELECT COALESCE(MAX(ts), 0) FROM stats_samples) - ?",
            (int(keep_seconds),),
        )


def upsert_client(pid, ip, operator, asn, sessions_inc, ts):
    with get_db() as db:
        db.execute(
            "INSERT INTO clients(proxy_id, ip, operator, asn, sessions, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(proxy_id, ip) DO UPDATE SET "
            "operator = excluded.operator, asn = excluded.asn, "
            "sessions = sessions + excluded.sessions, last_seen = excluded.last_seen",
            (pid, ip, operator, asn, max(0, int(sessions_inc)), int(ts), int(ts)),
        )


def proxy_stats(pid):
    with get_db() as db:
        return _row(db.execute("SELECT * FROM proxy_stats WHERE proxy_id=?", (pid,)).fetchone())


def latest_sample(pid):
    with get_db() as db:
        return _row(db.execute(
            "SELECT * FROM stats_samples WHERE proxy_id=? ORDER BY ts DESC, id DESC LIMIT 1", (pid,)
        ).fetchone())


def two_recent_samples(pid):
    with get_db() as db:
        return [_row(r) for r in db.execute(
            "SELECT * FROM stats_samples WHERE proxy_id=? ORDER BY ts DESC, id DESC LIMIT 2", (pid,)
        ).fetchall()]


def top_operators(pid=None, limit=6):
    with get_db() as db:
        if pid is None:
            rows = db.execute(
                "SELECT operator, COUNT(DISTINCT ip) c FROM clients "
                "GROUP BY operator ORDER BY c DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT operator, COUNT(DISTINCT ip) c FROM clients WHERE proxy_id=? "
                "GROUP BY operator ORDER BY c DESC LIMIT ?", (pid, limit)
            ).fetchall()
        return [_row(r) for r in rows]


def clients_count(pid=None):
    with get_db() as db:
        if pid is None:
            r = db.execute("SELECT COUNT(DISTINCT ip) c FROM clients").fetchone()
        else:
            r = db.execute("SELECT COUNT(DISTINCT ip) c FROM clients WHERE proxy_id=?", (pid,)).fetchone()
        return r["c"]
