"""FastAPI application: auth, dashboard, proxy CRUD, settings, stats."""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, cloudflare, config, database, failover, nettune, proxy_manager, stats, utils

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.init_db()
    # Seed the first admin from the environment, if not present yet.
    if config.ADMIN_PASSWORD and not database.get_admin(config.ADMIN_USER):
        database.create_admin(config.ADMIN_USER, auth.hash_password(config.ADMIN_PASSWORD))
    # Best-effort auto-detect of the public IP for link generation.
    if not database.get_setting("server_public_ip"):
        ip = utils.detect_public_ip()
        if ip:
            database.set_setting("server_public_ip", ip)

    # Background loops: statistics sampler + endpoint health/failover.
    tasks = []
    if config.STATS_ENABLED:
        tasks.append(asyncio.create_task(stats.sampler_loop()))
    if config.FAILOVER_ENABLED:
        tasks.append(asyncio.create_task(failover.failover_loop()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="MTProto Panel", lifespan=lifespan)
# same_site="lax" blocks the session cookie on cross-site POSTs -> CSRF mitigation.
app.add_middleware(
    SessionMiddleware, secret_key=config.SECRET_KEY, max_age=86400, same_site="lax", https_only=False
)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["VERSION"] = config.VERSION

# Front-end assets: served locally if vendored on the server (resilient to CDN
# filtering in Iran), otherwise fall back to a CDN URL.
_VENDOR = BASE / "static" / "vendor"


def _asset(name, cdn):
    return f"/static/vendor/{name}" if (_VENDOR / name).exists() else cdn


templates.env.globals["TABLER_CSS"] = _asset(
    "tabler.rtl.min.css", "https://cdn.jsdelivr.net/npm/@tabler/core@latest/dist/css/tabler.rtl.min.css"
)
templates.env.globals["TABLER_JS"] = _asset(
    "tabler.min.js", "https://cdn.jsdelivr.net/npm/@tabler/core@latest/dist/js/tabler.min.js"
)
templates.env.globals["APEX_JS"] = _asset(
    "apexcharts.min.js", "https://cdn.jsdelivr.net/npm/apexcharts@latest/dist/apexcharts.min.js"
)
templates.env.globals["QR_JS"] = _asset(
    "qrcode.min.js", "https://cdn.jsdelivr.net/gh/davidshimjs/qrcodejs@gh-pages/qrcode.min.js"
)


# --- Auth plumbing --------------------------------------------------------
class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def _redirect_login(_request: Request, _exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user


# --- Auth routes ----------------------------------------------------------
@app.get("/login")
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    admin = database.get_admin(username)
    if admin and auth.verify_password(password, admin["pw_hash"]):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "نام کاربری یا رمز عبور اشتباه است."}, status_code=401
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Dashboard ------------------------------------------------------------
def _safe_next(value, default="/"):
    """Only allow same-site relative redirects."""
    return value if isinstance(value, str) and value.startswith("/") and not value.startswith("//") else default


def _resolve_link(p, ip):
    """Host precedence for the user-facing link. Returns (host, link_port, links, active_endpoint)."""
    active_ep = database.get_active_endpoint(p["id"])
    if p["mode"] == "cloudflare" and p["cf_domain"]:
        host, link_port = p["cf_domain"], (p["cf_edge_port"] or 443)       # Spectrum edge
    elif p.get("link_domain"):
        host, link_port = p["link_domain"], p["port"]                       # managed domain (DNS failover)
    elif active_ep:
        host, link_port = active_ep["address"], p["port"]                   # active rotation endpoint
    else:
        host, link_port = (p.get("front_host") or ip), p["port"]            # tunnel front / origin IP
    links = utils.build_links(host or "SERVER_IP", link_port, p["secret"], p["tls_domain"])
    return host, link_port, links, active_ep


def _build_rows():
    ip = database.get_setting("server_public_ip", "") or ""
    proxies = database.list_proxies()
    statuses = proxy_manager.statuses(proxies)  # one systemctl call for all
    rows = []
    for p in proxies:
        host, link_port, links, active_ep = _resolve_link(p, ip)
        rows.append({
            **p, "host": host, "link_port": link_port,
            "status": statuses.get(p["id"], "unknown"), "active_endpoint": active_ep, **links,
        })
    return rows, ip


@app.get("/")
def dashboard(request: Request, _user: str = Depends(require_user)):
    rows, ip = _build_rows()
    totals = stats.snapshot()["totals"]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, "proxies": rows, "ip": ip,
            "nettune": nettune.status(), "totals": totals, "hb": utils.human_bytes,
        },
    )


# --- Proxy create ---------------------------------------------------------
@app.get("/proxies/new")
def new_proxy_form(request: Request, _user: str = Depends(require_user)):
    return templates.TemplateResponse(
        "proxy_new.html", {"request": request, "suggest_secret": utils.gen_secret(), "error": None}
    )


@app.post("/proxies")
def create_proxy(
    request: Request,
    _user: str = Depends(require_user),
    name: str = Form(...),
    mode: str = Form(...),
    port: str = Form(...),
    tls_domain: str = Form("www.cloudflare.com"),
    secret: str = Form(""),
    ad_tag: str = Form(""),
    upstream_host: str = Form(""),
    upstream_port: str = Form(""),
    cf_domain: str = Form(""),
    cf_edge_port: str = Form("443"),
    front_host: str = Form(""),
):
    def fail(msg):
        return templates.TemplateResponse(
            "proxy_new.html",
            {"request": request, "suggest_secret": utils.gen_secret(), "error": msg},
            status_code=400,
        )

    if mode not in ("direct", "tunneled", "cloudflare"):
        return fail("حالت اتصال نامعتبر است.")
    if not utils.valid_port(port):
        return fail("پورت نامعتبر است.")
    port = int(port)
    if database.port_in_use(port):
        return fail("این پورت قبلاً استفاده شده است.")
    if not utils.valid_domain(tls_domain):
        return fail("دامنه FakeTLS نامعتبر است.")

    try:
        raw_secret = utils.normalize_secret(secret) if secret.strip() else utils.gen_secret()
    except ValueError as e:
        return fail(str(e))

    ad = ad_tag.strip().lower() or None
    if ad and not re.fullmatch(r"[0-9a-f]{32}", ad):
        return fail("تگ اسپانسر باید ۳۲ کاراکتر هگز باشد (از @MTProxybot).")

    fh = front_host.strip()
    if fh and not utils.valid_host(fh):
        return fail("آدرس نمایشی (Front) نامعتبر است.")

    data = {
        "name": name.strip() or f"proxy-{port}",
        "mode": mode,
        "port": int(port),
        "secret": raw_secret,
        "tls_domain": tls_domain.strip(),
        "ad_tag": ad,
        "upstream_host": None,
        "upstream_port": None,
        "cf_domain": None,
        "cf_edge_port": 443,
        "front_host": fh or None,
        "enabled": 1,
    }

    if mode == "tunneled":
        if not utils.valid_host(upstream_host) or not utils.valid_port(upstream_port):
            return fail("برای حالت تانل، آدرس/دامنه و پورت سرور خارج باید معتبر باشد.")
        data["upstream_host"] = upstream_host.strip()
        data["upstream_port"] = int(upstream_port)

    if mode == "cloudflare":
        if not utils.valid_domain(cf_domain):
            return fail("برای حالت کلودفلر، دامنه‌ی edge الزامی است.")
        if not utils.valid_port(cf_edge_port):
            return fail("پورت edge کلودفلر نامعتبر است.")
        data["cf_domain"] = cf_domain.strip()
        data["cf_edge_port"] = int(cf_edge_port)

    pid = database.create_proxy(data)
    proxy = database.get_proxy(pid)
    proxy_manager.apply(proxy)

    # Best-effort Cloudflare Spectrum provisioning.
    if mode == "cloudflare":
        token = database.get_setting("cf_api_token")
        zone = database.get_setting("cf_zone_id")
        origin_ip = database.get_setting("server_public_ip")
        if token and zone and origin_ip:
            ok, detail = cloudflare.create_spectrum_app(
                token, zone, data["cf_domain"], data["cf_edge_port"], origin_ip, data["port"]
            )
            if ok:
                app_id = (detail.get("result") or {}).get("id")
                if app_id:
                    database.set_cf_app_id(pid, app_id)

    return RedirectResponse("/", status_code=303)


@app.post("/proxies/{pid}/toggle")
def toggle_proxy(pid: int, _user: str = Depends(require_user), back: str = Form("/")):
    p = database.get_proxy(pid)
    if p:
        if p["enabled"]:
            proxy_manager.stop(p)
            database.set_enabled(pid, 0)
        else:
            database.set_enabled(pid, 1)
            proxy_manager.apply(database.get_proxy(pid))
    return RedirectResponse(_safe_next(back), status_code=303)


@app.post("/proxies/{pid}/delete")
def delete_proxy(pid: int, _user: str = Depends(require_user)):
    p = database.get_proxy(pid)
    if p:
        # Clean up the Cloudflare Spectrum app so we don't leak a paid resource.
        if p["mode"] == "cloudflare" and p.get("cf_app_id"):
            token = database.get_setting("cf_api_token")
            zone = database.get_setting("cf_zone_id")
            if token and zone:
                cloudflare.delete_spectrum_app(token, zone, p["cf_app_id"])
        proxy_manager.remove(p)
        database.delete_proxy(pid)
        stats.forget(pid)
    return RedirectResponse("/", status_code=303)


# --- Settings -------------------------------------------------------------
@app.get("/settings")
def settings_form(request: Request, _user: str = Depends(require_user)):
    ctx = {
        "request": request,
        "server_public_ip": database.get_setting("server_public_ip", ""),
        "cf_api_token": database.get_setting("cf_api_token", ""),
        "cf_zone_id": database.get_setting("cf_zone_id", ""),
        "message": None,
        "error": None,
    }
    return templates.TemplateResponse("settings.html", ctx)


@app.post("/settings")
def save_settings(
    request: Request,
    _user: str = Depends(require_user),
    server_public_ip: str = Form(""),
    cf_api_token: str = Form(""),
    cf_zone_id: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    message, error = None, None
    database.set_setting("server_public_ip", server_public_ip.strip())
    database.set_setting("cf_api_token", cf_api_token.strip())
    database.set_setting("cf_zone_id", cf_zone_id.strip())

    if new_password:
        if new_password != new_password2:
            error = "رمز عبور و تکرار آن یکسان نیستند."
        elif len(new_password) < 8:
            error = "رمز عبور باید حداقل ۸ کاراکتر باشد."
        else:
            database.update_admin_password(request.session["user"], auth.hash_password(new_password))
            message = "تنظیمات و رمز عبور ذخیره شد."
    if not error and not message:
        message = "تنظیمات ذخیره شد."

    ctx = {
        "request": request,
        "server_public_ip": database.get_setting("server_public_ip", ""),
        "cf_api_token": database.get_setting("cf_api_token", ""),
        "cf_zone_id": database.get_setting("cf_zone_id", ""),
        "message": message,
        "error": error,
    }
    return templates.TemplateResponse("settings.html", ctx)


# --- Statistics -----------------------------------------------------------
@app.get("/stats")
def stats_page(request: Request, _user: str = Depends(require_user)):
    return templates.TemplateResponse(
        "stats.html",
        {"request": request, "snap": stats.snapshot(), "hb": utils.human_bytes, "hr": utils.human_rate},
    )


@app.get("/api/stats")
def stats_api(_user: str = Depends(require_user)):
    return JSONResponse(stats.snapshot())


@app.get("/api/series")
def series_api(_user: str = Depends(require_user)):
    return JSONResponse(stats.series())


# --- Per-proxy detail / management ---------------------------------------
@app.get("/proxies/{pid}")
def proxy_detail(pid: int, request: Request, _user: str = Depends(require_user)):
    p = database.get_proxy(pid)
    if not p:
        return RedirectResponse("/", status_code=303)
    ip = database.get_setting("server_public_ip", "") or ""
    host, link_port, links, active_ep = _resolve_link(p, ip)
    ctx = {
        "request": request,
        "p": {**p, "host": host, "link_port": link_port, "status": proxy_manager.status(p), **links},
        "endpoints": database.list_endpoints(pid),
        "snap": stats.proxy_snapshot(pid),
        "hb": utils.human_bytes, "hr": utils.human_rate,
        "cf_dns_ready": bool(database.get_setting("cf_api_token") and database.get_setting("cf_zone_id")),
        "flash": request.session.pop("flash", None),
    }
    return templates.TemplateResponse("proxy_detail.html", ctx)


@app.post("/proxies/{pid}/edit")
def edit_proxy(pid: int, request: Request, _user: str = Depends(require_user),
               name: str = Form(...), tls_domain: str = Form(...),
               ad_tag: str = Form(""), front_host: str = Form("")):
    p = database.get_proxy(pid)
    if not p:
        return RedirectResponse("/", status_code=303)
    back = f"/proxies/{pid}"
    if not utils.valid_domain(tls_domain):
        request.session["flash"] = "دامنه FakeTLS نامعتبر است."
        return RedirectResponse(back, status_code=303)
    ad = ad_tag.strip().lower() or None
    if ad and not re.fullmatch(r"[0-9a-f]{32}", ad):
        request.session["flash"] = "تگ اسپانسر باید ۳۲ کاراکتر هگز باشد."
        return RedirectResponse(back, status_code=303)
    fh = front_host.strip()
    if fh and not utils.valid_host(fh):
        request.session["flash"] = "آدرس نمایشی (Front) نامعتبر است."
        return RedirectResponse(back, status_code=303)
    database.update_proxy_settings(pid, name.strip() or p["name"], tls_domain.strip(), ad, fh or None)
    proxy_manager.apply(database.get_proxy(pid))  # regenerate config + restart so changes take effect
    request.session["flash"] = "تغییرات ذخیره و اعمال شد."
    return RedirectResponse(back, status_code=303)


@app.get("/api/proxies/{pid}/stats")
def proxy_stats_api(pid: int, _user: str = Depends(require_user)):
    return JSONResponse(stats.proxy_snapshot(pid))


@app.get("/api/proxies/{pid}/series")
def proxy_series_api(pid: int, _user: str = Depends(require_user)):
    return JSONResponse(stats.series(proxy_id=pid))


# --- Anti-filter: endpoints, rotation, DNS failover ----------------------
@app.get("/antifilter")
def antifilter_page(request: Request, _user: str = Depends(require_user)):
    items = [{**p, "endpoints": database.list_endpoints(p["id"])} for p in database.list_proxies()]
    ctx = {
        "request": request,
        "items": items,
        "cf_dns_ready": bool(database.get_setting("cf_api_token") and database.get_setting("cf_zone_id")),
        "flash": request.session.pop("flash", None),
    }
    return templates.TemplateResponse("antifilter.html", ctx)


@app.post("/antifilter/{pid}/endpoints")
def add_endpoint(pid: int, request: Request, _user: str = Depends(require_user),
                 address: str = Form(...), label: str = Form(""), priority: str = Form("100"),
                 back: str = Form("/antifilter")):
    if not database.get_proxy(pid):
        return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)
    if not utils.valid_host(address):
        request.session["flash"] = "آدرس endpoint نامعتبر است (IP یا دامنه)."
    else:
        pr = int(priority) if priority.strip().isdigit() else 100
        database.add_endpoint(pid, address.strip(), label.strip() or None, pr)
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/endpoints/{eid}/delete")
def del_endpoint(eid: int, _user: str = Depends(require_user), back: str = Form("/antifilter")):
    database.delete_endpoint(eid)
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/endpoints/{eid}/activate")
def activate_endpoint(eid: int, request: Request, _user: str = Depends(require_user),
                      back: str = Form("/antifilter")):
    ep = database.get_endpoint(eid)
    if ep:
        failover.rotate(ep["proxy_id"], prefer_id=eid)
        request.session["flash"] = f"endpoint فعال شد: {ep['address']}"
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/{pid}/rotate")
def rotate_proxy(pid: int, request: Request, _user: str = Depends(require_user),
                 back: str = Form("/antifilter")):
    tgt = failover.rotate(pid)
    request.session["flash"] = (f"چرخش انجام شد → {tgt['address']}" if tgt else "endpoint سالمی برای چرخش نبود.")
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/{pid}/domain")
def set_domain(pid: int, request: Request, _user: str = Depends(require_user),
               link_domain: str = Form(""), auto_rotate: str = Form(""), back: str = Form("/antifilter")):
    d = link_domain.strip()
    if d and not utils.valid_domain(d):
        request.session["flash"] = "دامنه نامعتبر است."
        return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)
    database.set_link_domain(pid, d or None)
    database.set_auto_rotate(pid, bool(auto_rotate))
    # Point the domain at the current active endpoint right away.
    proxy = database.get_proxy(pid)
    active = database.get_active_endpoint(pid)
    if d and active:
        ok, _ = failover.sync_dns(proxy, active)
        request.session["flash"] = "دامنه ذخیره شد و DNS به‌روزرسانی شد." if ok else "دامنه ذخیره شد (به‌روزرسانی DNS انجام نشد — تنظیمات کلودفلر را چک کن)."
    else:
        request.session["flash"] = "ذخیره شد."
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/endpoints/{eid}/iran-test")
def iran_test_endpoint(eid: int, request: Request, _user: str = Depends(require_user),
                       back: str = Form("/antifilter")):
    ep = database.get_endpoint(eid)
    if ep:
        proxy = database.get_proxy(ep["proxy_id"])
        res = failover.iran_test(ep["address"], proxy["port"])
        if res["ok"]:
            request.session["flash"] = (
                f"تست از ایران برای {ep['address']}: {res['reachable']} از {res['total']} نود ایرانی وصل شدند"
                + (" ✅" if res["reachable"] else " ❌ (احتمالاً فیلتر/خاموش)")
            )
        else:
            request.session["flash"] = f"تست از ایران ناموفق بود: {res['error']}"
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/{pid}/spectrum/recreate")
def spectrum_recreate(pid: int, request: Request, _user: str = Depends(require_user),
                      back: str = Form("/antifilter")):
    p = database.get_proxy(pid)
    token = database.get_setting("cf_api_token")
    zone = database.get_setting("cf_zone_id")
    origin_ip = database.get_setting("server_public_ip")
    if p and p["mode"] == "cloudflare" and p["cf_domain"] and token and zone and origin_ip:
        ok, detail = cloudflare.create_spectrum_app(
            token, zone, p["cf_domain"], p["cf_edge_port"], origin_ip, p["port"]
        )
        if ok:
            app_id = (detail.get("result") or {}).get("id")
            if app_id:
                database.set_cf_app_id(pid, app_id)
        request.session["flash"] = "اپ Spectrum ساخته شد." if ok else f"ساخت Spectrum ناموفق: {detail}"
    else:
        request.session["flash"] = "برای Spectrum: پروکسی باید حالت کلودفلر باشد و توکن/Zone/IP تنظیم شده باشند."
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)


@app.post("/antifilter/{pid}/spectrum/delete")
def spectrum_delete(pid: int, request: Request, _user: str = Depends(require_user),
                    back: str = Form("/antifilter")):
    p = database.get_proxy(pid)
    token = database.get_setting("cf_api_token")
    zone = database.get_setting("cf_zone_id")
    if p and p.get("cf_app_id") and token and zone:
        cloudflare.delete_spectrum_app(token, zone, p["cf_app_id"])
        database.set_cf_app_id(pid, None)
        request.session["flash"] = "اپ Spectrum حذف شد."
    return RedirectResponse(_safe_next(back, "/antifilter"), status_code=303)
