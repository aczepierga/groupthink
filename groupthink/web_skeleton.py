"""Deployable skeleton: home page, health check and hosting probes.

Real app routes arrive in Task 8. The /_probe/* routes exist to learn what the
host does (client IP headers, egress, cookies, KV) and are 404 unless the env
var PROBES_ENABLED is exactly "1". No probe ever returns a secret value.
"""
import os
from pathlib import Path

import jinja2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import config
from .kv import Kv, KvError
from .ls_client import urllib_transport

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "gt_probe"
COUNT_KEY = "gt:probe:count"
EGRESS_URL = config.BASE_URL + "/ls_agent_api/status"
EGRESS_TIMEOUT_S = 10


def _probes_enabled():
    if os.environ.get("PROBES_ENABLED") != "1":
        raise HTTPException(status_code=404)


def create_app(kv_factory=Kv.from_env, egress_transport=urllib_transport):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             autoescape=jinja2.select_autoescape(["html"]))
    templates = Jinja2Templates(env=env)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(request, "home.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    probes = APIRouter(prefix="/_probe", dependencies=[Depends(_probes_enabled)])

    @probes.get("/whoami")
    def whoami(request: Request):
        h = request.headers
        return {"client_host": request.client.host if request.client else None,
                "x_forwarded_for": h.get("x-forwarded-for"),
                "x_real_ip": h.get("x-real-ip"),
                "x_vercel_forwarded_for": h.get("x-vercel-forwarded-for")}

    @probes.get("/egress")
    def egress():
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        try:
            status, _, _ = egress_transport("GET", EGRESS_URL, headers, None, EGRESS_TIMEOUT_S)
        except Exception:
            status = 0
        return {"status": status}

    @probes.get("/cookie")
    def cookie(request: Request):
        had = request.cookies.get(COOKIE_NAME) == "1"
        response = JSONResponse({"had_cookie": had})
        if not had:
            response.set_cookie(COOKIE_NAME, "1", httponly=True, secure=True, samesite="lax")
        return response

    @probes.get("/count")
    def count():
        kv = kv_factory()
        if kv is None:
            return {"count": None, "kv": "not configured"}
        try:
            return {"count": kv.incr(COUNT_KEY)}
        except KvError:
            return JSONResponse({"count": None, "kv": "error"}, status_code=502)

    @probes.get("/canary")
    def canary():
        value = os.environ.get("CANARY_KEY", "")
        return {"set": bool(value), "len": len(value)}

    app.include_router(probes)
    return app


app = create_app()
