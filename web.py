import base64
import json
import time
import hmac
import hashlib
import secrets
from urllib.parse import urlencode
import aiohttp
from aiohttp import web
import aiohttp_jinja2
import jinja2

import config
from database import get_db

# ================= SESSION HELPERS =================
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def sign_session(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(config.SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(signature)}"

def read_session(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(config.SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _b64d(signature)):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload

async def current_user(request: web.Request) -> dict | None:
    payload = read_session(request.cookies.get(config.SESSION_COOKIE))
    if not payload:
        return None
    
    async with await get_db() as db:
        async with db.execute("SELECT * FROM employees WHERE discord_id = ?", (payload["discord_id"],)) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        return {
            "discord_id": payload["discord_id"],
            "username": payload.get("username"),
            "role": "guest",
        }
    return {
        "discord_id": row["discord_id"],
        "username": payload.get("username"),
        "role": "admin" if row["is_admin"] else "employee",
    }

# ================= PAGE HANDLERS (แก้ปัญหาที่ขาดหายไป) =================
@aiohttp_jinja2.template('index.html')
async def page_index(request: web.Request):
    user = await current_user(request)
    return {'user': user}

@aiohttp_jinja2.template('operatives.html')
async def page_operatives(request: web.Request):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM employees ORDER BY points DESC") as cursor:
            employees = await cursor.fetchall()
    return {'employees': employees}

@aiohttp_jinja2.template('archives.html')
async def page_archives(request: web.Request):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM games ORDER BY created_at DESC") as cursor:
            games = await cursor.fetchall()
    return {'games': games}

@aiohttp_jinja2.template('store.html')
async def page_store(request: web.Request):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM store_items WHERE is_active = 1") as cursor:
            items = await cursor.fetchall()
    return {'items': items}

@aiohttp_jinja2.template('admin.html')
async def page_admin(request: web.Request):
    user = await current_user(request)
    if not user or user.get("role") != "admin":
        raise web.HTTPFound("/")
    return {'user': user}

# ================= AUTH HANDLERS =================
async def auth_login(request: web.Request) -> web.StreamResponse:
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "consent",
    }
    response = web.HTTPFound(f"{config.DISCORD_API}/oauth2/authorize?{urlencode(params)}")
    response.set_cookie("societ_oauth_state", state, max_age=600, httponly=True)
    return response

async def auth_callback(request: web.Request) -> web.StreamResponse:
    code = request.query.get("code")
    if not code:
        return web.json_response({"error": "missing_code"}, status=400)
    
    data = {
        "client_id": config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{config.DISCORD_API}/oauth2/token", data=data) as resp:
            token_data = await resp.json()
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        async with session.get(f"{config.DISCORD_API}/users/@me", headers=headers) as resp:
            profile = await resp.json()

    payload = {
        "discord_id": str(profile["id"]),
        "username": profile.get("global_name") or profile.get("username"),
        "exp": int(time.time()) + config.SESSION_TTL,
    }
    response = web.HTTPFound("/")
    response.set_cookie(config.SESSION_COOKIE, sign_session(payload), max_age=config.SESSION_TTL, httponly=True)
    return response

async def auth_logout(request: web.Request) -> web.StreamResponse:
    response = web.HTTPFound("/")
    response.del_cookie(config.SESSION_COOKIE)
    return response

# ================= APP SETUP =================
def build_app() -> web.Application:
    app = web.Application()
    
    # Setup Jinja2 Templates
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(config.BASE_DIR / 'templates'))
    
    app.add_routes([
        web.get("/", page_index),
        web.get("/operatives", page_operatives),
        web.get("/archives", page_archives),
        web.get("/store", page_store),
        web.get("/admin", page_admin),
        web.get("/login", auth_login),
        web.get("/auth/callback", auth_callback),
        web.get("/logout", auth_logout),
    ])

    static_dir = config.BASE_DIR / "static"
    if static_dir.exists():
        app.router.add_static("/static/", static_dir)
        
    return app
# เพิ่มบรรทัดนี้ไว้ท้ายสุดของ web.py
app = build_app()
