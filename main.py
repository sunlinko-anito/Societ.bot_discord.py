"""Societ Game Studio - Discord bot + REST API backend.

Runs a discord.py bot and an aiohttp web server in the same event loop:

* Slash commands: /test, /work, /rd_employee, /add_employee, /points check|give
* REST API for the web frontend (roster, portfolio, store, redemptions)
* Discord OAuth2 login with signed-cookie sessions and RBAC (guest/employee/admin)
* SQLite3 persistence in database.db
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = os.getenv("SOCIET_DB", str(BASE_DIR / "database.db"))

TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/auth/callback")
ADMIN_WEBHOOK_URL = os.getenv("ADMIN_WEBHOOK_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
SESSION_COOKIE = "societ_session"
SESSION_TTL = 60 * 60 * 24 * 7
WEB_HOST = os.getenv("HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "5000"))
# Origins allowed to call the API with credentials, e.g. the standalone Societ-web site.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

DISCORD_API = "https://discord.com/api/v10"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# --------------------------------------------------------------------------------------
# DATABASE
# --------------------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create every table used by the bot and the web API."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT UNIQUE NOT NULL,
            nickname TEXT NOT NULL,
            position TEXT NOT NULL,
            bio TEXT,
            contact_email TEXT,
            points INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_visible INTEGER DEFAULT 1
        )
    """)

    # Databases created before profile visibility existed.
    columns = {row["name"] for row in cursor.execute("PRAGMA table_info(employees)")}
    if "is_visible" not in columns:
        cursor.execute("ALTER TABLE employees ADD COLUMN is_visible INTEGER DEFAULT 1")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            platforms TEXT,
            image_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS store_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            price_points INTEGER NOT NULL,
            stock INTEGER DEFAULT -1,
            image_url TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            points_spent INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(discord_id) REFERENCES employees(discord_id),
            FOREIGN KEY(item_id) REFERENCES store_items(id)
        )
    """)

    conn.commit()
    conn.close()
    print("[Database] Tables ready in", DB_NAME)


def fetch_employee(discord_id: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (str(discord_id),)).fetchone()
    conn.close()
    return row


def employee_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "discord_id": row["discord_id"],
        "nickname": row["nickname"],
        "position": row["position"],
        "bio": row["bio"],
        "contact_email": row["contact_email"],
        "points": row["points"],
        "is_admin": bool(row["is_admin"]),
        "is_visible": bool(row["is_visible"]),
    }


# --------------------------------------------------------------------------------------
# SESSION HANDLING (signed cookies, no external dependency)
# --------------------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_session(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(signature)}"


def read_session(token: Optional[str]) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _b64d(signature)):
            return None
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def current_user(request: web.Request) -> Optional[dict]:
    """Return the DB-backed profile of the logged-in user, or None for guests."""
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if not payload:
        return None
    row = fetch_employee(payload["discord_id"])
    if not row:
        # Authenticated on Discord but not on staff: treated as a guest with an identity.
        return {
            "discord_id": payload["discord_id"],
            "username": payload.get("username"),
            "avatar_url": payload.get("avatar_url"),
            "role": "guest",
            "employee": None,
        }
    return {
        "discord_id": row["discord_id"],
        "username": payload.get("username"),
        "avatar_url": payload.get("avatar_url"),
        "role": "admin" if row["is_admin"] else "employee",
        "employee": employee_public(row),
    }


Handler = Callable[[web.Request], Coroutine[Any, Any, web.StreamResponse]]


def require_employee(handler: Handler) -> Handler:
    async def wrapper(request: web.Request) -> web.StreamResponse:
        user = current_user(request)
        if not user or user["role"] == "guest":
            raise web.HTTPUnauthorized(text=json.dumps({"error": "employee_login_required"}),
                                       content_type="application/json")
        request["user"] = user
        return await handler(request)
    return wrapper


def require_admin(handler: Handler) -> Handler:
    async def wrapper(request: web.Request) -> web.StreamResponse:
        user = current_user(request)
        if not user or user["role"] != "admin":
            raise web.HTTPForbidden(text=json.dumps({"error": "admin_only"}),
                                    content_type="application/json")
        request["user"] = user
        return await handler(request)
    return wrapper


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}),
                                 content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}),
                                 content_type="application/json")
    return data


# --------------------------------------------------------------------------------------
# DISCORD OAUTH2
# --------------------------------------------------------------------------------------

async def auth_login(request: web.Request) -> web.StreamResponse:
    if not CLIENT_ID or not CLIENT_SECRET:
        return web.json_response({"error": "oauth_not_configured"}, status=503)
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "consent",
    }
    response = web.HTTPFound(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")
    response.set_cookie("societ_oauth_state", state, max_age=600, httponly=True, samesite="Lax")
    raise response


async def auth_callback(request: web.Request) -> web.StreamResponse:
    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state or state != request.cookies.get("societ_oauth_state"):
        return web.json_response({"error": "invalid_oauth_state"}, status=400)

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT_URI,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{DISCORD_API}/oauth2/token", data=data) as resp:
            if resp.status != 200:
                return web.json_response({"error": "token_exchange_failed"}, status=502)
            token_data = await resp.json()
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        async with session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
            if resp.status != 200:
                return web.json_response({"error": "profile_fetch_failed"}, status=502)
            profile = await resp.json()

    avatar = profile.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{profile['id']}/{avatar}.png"
        if avatar
        else f"https://cdn.discordapp.com/embed/avatars/{(int(profile['id']) >> 22) % 6}.png"
    )
    payload = {
        "discord_id": str(profile["id"]),
        "username": profile.get("global_name") or profile.get("username"),
        "avatar_url": avatar_url,
        "exp": int(time.time()) + SESSION_TTL,
    }
    response = web.HTTPFound("/")
    response.set_cookie(SESSION_COOKIE, sign_session(payload), max_age=SESSION_TTL,
                        httponly=True, samesite="Lax")
    response.del_cookie("societ_oauth_state")
    raise response


async def auth_logout(request: web.Request) -> web.StreamResponse:
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE)
    return response


async def api_me(request: web.Request) -> web.StreamResponse:
    user = current_user(request)
    if not user:
        return web.json_response({"authenticated": False, "role": "guest"})
    return web.json_response({"authenticated": True, **user})


# --------------------------------------------------------------------------------------
# PUBLIC REST API
# --------------------------------------------------------------------------------------

async def api_employees(request: web.Request) -> web.StreamResponse:
    """Public roster. Hidden profiles are only returned to admins and to their owner."""
    user = current_user(request)
    conn = get_conn()
    rows = conn.execute("SELECT * FROM employees ORDER BY is_admin DESC, nickname ASC").fetchall()
    conn.close()

    is_admin = bool(user and user["role"] == "admin")
    own_id = user["discord_id"] if user else None
    return web.json_response([
        employee_public(row) for row in rows
        if row["is_visible"] or is_admin or row["discord_id"] == own_id
    ])


async def api_games(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM games ORDER BY created_at DESC, id DESC").fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


async def api_store_items(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM store_items WHERE is_active = 1 ORDER BY price_points ASC"
    ).fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


async def send_admin_webhook(content: str, embed: Optional[dict] = None) -> None:
    if not ADMIN_WEBHOOK_URL:
        return
    payload: dict = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(ADMIN_WEBHOOK_URL, json=payload)
    except aiohttp.ClientError as exc:
        print(f"[Webhook] failed to notify admins: {exc}")


@require_employee
async def api_store_redeem(request: web.Request) -> web.StreamResponse:
    body = await read_json(request)
    try:
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "item_id_required"}, status=400)

    discord_id = request["user"]["discord_id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT * FROM store_items WHERE id = ? AND is_active = 1", (item_id,)
        ).fetchone()
        if not item:
            conn.rollback()
            return web.json_response({"error": "item_not_found"}, status=404)
        if item["stock"] == 0:
            conn.rollback()
            return web.json_response({"error": "out_of_stock"}, status=409)

        employee = conn.execute(
            "SELECT * FROM employees WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if employee["points"] < item["price_points"]:
            conn.rollback()
            return web.json_response({"error": "insufficient_points",
                                      "points": employee["points"],
                                      "required": item["price_points"]}, status=402)

        conn.execute("UPDATE employees SET points = points - ? WHERE discord_id = ?",
                     (item["price_points"], discord_id))
        if item["stock"] > 0:
            conn.execute("UPDATE store_items SET stock = stock - 1 WHERE id = ?", (item_id,))
        conn.execute(
            "INSERT INTO transactions (discord_id, item_id, points_spent) VALUES (?, ?, ?)",
            (discord_id, item_id, item["price_points"]),
        )
        conn.commit()
        remaining = conn.execute(
            "SELECT points FROM employees WHERE discord_id = ?", (discord_id,)
        ).fetchone()["points"]
    finally:
        conn.close()

    await send_admin_webhook(
        "🛒 **Store redemption**",
        {
            "title": f"{employee['nickname']} redeemed {item['title']}",
            "description": (
                f"**Operative:** <@{discord_id}> ({employee['position']})\n"
                f"**Item:** {item['title']}\n"
                f"**Cost:** {item['price_points']} pts\n"
                f"**Remaining balance:** {remaining} pts"
            ),
            "color": 0x34D399,
        },
    )
    return web.json_response({"ok": True, "points": remaining, "item": dict(item)})


@require_employee
async def api_my_transactions(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.points_spent, t.created_at, s.title
           FROM transactions t LEFT JOIN store_items s ON s.id = t.item_id
           WHERE t.discord_id = ? ORDER BY t.created_at DESC LIMIT 50""",
        (request["user"]["discord_id"],),
    ).fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


@require_employee
async def api_update_profile(request: web.Request) -> web.StreamResponse:
    """Self-service editing of the caller's own bio / nickname / position / contact."""
    body = await read_json(request)
    fields = {key: body[key] for key in ("nickname", "position", "bio", "contact_email")
              if key in body and body[key] is not None}
    if "is_visible" in body:
        fields["is_visible"] = int(bool(body["is_visible"]))
    if not fields:
        return web.json_response({"error": "nothing_to_update"}, status=400)

    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn = get_conn()
    conn.execute(f"UPDATE employees SET {assignments} WHERE discord_id = ?",
                 (*fields.values(), request["user"]["discord_id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?",
                       (request["user"]["discord_id"],)).fetchone()
    conn.close()
    return web.json_response(employee_public(row))


# --------------------------------------------------------------------------------------
# ADMIN REST API
# --------------------------------------------------------------------------------------

@require_admin
async def admin_upsert_employee(request: web.Request) -> web.StreamResponse:
    body = await read_json(request)
    discord_id = str(body.get("discord_id", "")).strip()
    nickname = (body.get("nickname") or "").strip()
    position = (body.get("position") or "").strip()
    if not discord_id or not nickname or not position:
        return web.json_response({"error": "discord_id_nickname_position_required"}, status=400)

    conn = get_conn()
    conn.execute(
        """INSERT INTO employees
               (discord_id, nickname, position, bio, contact_email, points, is_admin, is_visible)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET
               nickname = excluded.nickname,
               position = excluded.position,
               bio = excluded.bio,
               contact_email = excluded.contact_email,
               points = excluded.points,
               is_admin = excluded.is_admin,
               is_visible = excluded.is_visible""",
        (discord_id, nickname, position, body.get("bio"), body.get("contact_email"),
         int(body.get("points") or 0), int(bool(body.get("is_admin"))),
         int(bool(body.get("is_visible", True)))),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (discord_id,)).fetchone()
    conn.close()
    return web.json_response(employee_public(row))


@require_admin
async def admin_delete_employee(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    conn.execute("DELETE FROM employees WHERE id = ?", (int(request.match_info["emp_id"]),))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})


@require_admin
async def admin_adjust_points(request: web.Request) -> web.StreamResponse:
    body = await read_json(request)
    discord_id = str(body.get("discord_id", "")).strip()
    try:
        delta = int(body.get("delta"))
    except (TypeError, ValueError):
        return web.json_response({"error": "delta_required"}, status=400)

    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (discord_id,)).fetchone()
    if not row:
        conn.close()
        return web.json_response({"error": "employee_not_found"}, status=404)
    conn.execute("UPDATE employees SET points = MAX(0, points + ?) WHERE discord_id = ?",
                 (delta, discord_id))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (discord_id,)).fetchone()
    conn.close()
    return web.json_response(employee_public(row))


@require_admin
async def admin_upsert_game(request: web.Request) -> web.StreamResponse:
    body = await read_json(request)
    title = (body.get("title") or "").strip()
    status = (body.get("status") or "").strip().upper()
    if not title or status not in ("IN DEVELOPMENT", "RELEASED", "PROTOTYPE"):
        return web.json_response({"error": "title_and_valid_status_required"}, status=400)

    conn = get_conn()
    if body.get("id"):
        conn.execute(
            """UPDATE games SET title = ?, description = ?, status = ?, platforms = ?, image_url = ?
               WHERE id = ?""",
            (title, body.get("description"), status, body.get("platforms"),
             body.get("image_url"), int(body["id"])),
        )
        game_id = int(body["id"])
    else:
        cursor = conn.execute(
            """INSERT INTO games (title, description, status, platforms, image_url)
               VALUES (?, ?, ?, ?, ?)""",
            (title, body.get("description"), status, body.get("platforms"), body.get("image_url")),
        )
        game_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    conn.close()
    return web.json_response(dict(row))


@require_admin
async def admin_delete_game(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    conn.execute("DELETE FROM games WHERE id = ?", (int(request.match_info["game_id"]),))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})


@require_admin
async def admin_list_store_items(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM store_items ORDER BY id DESC").fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


@require_admin
async def admin_upsert_store_item(request: web.Request) -> web.StreamResponse:
    body = await read_json(request)
    title = (body.get("title") or "").strip()
    try:
        price = int(body.get("price_points"))
    except (TypeError, ValueError):
        return web.json_response({"error": "price_points_required"}, status=400)
    if not title or price < 0:
        return web.json_response({"error": "title_and_price_required"}, status=400)

    stock = int(body.get("stock", -1))
    is_active = int(bool(body.get("is_active", True)))
    conn = get_conn()
    if body.get("id"):
        conn.execute(
            """UPDATE store_items
               SET title = ?, description = ?, price_points = ?, stock = ?, image_url = ?, is_active = ?
               WHERE id = ?""",
            (title, body.get("description"), price, stock, body.get("image_url"),
             is_active, int(body["id"])),
        )
        item_id = int(body["id"])
    else:
        cursor = conn.execute(
            """INSERT INTO store_items (title, description, price_points, stock, image_url, is_active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, body.get("description"), price, stock, body.get("image_url"), is_active),
        )
        item_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM store_items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return web.json_response(dict(row))


@require_admin
async def admin_delete_store_item(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    conn.execute("DELETE FROM store_items WHERE id = ?", (int(request.match_info["item_id"]),))
    conn.commit()
    conn.close()
    return web.json_response({"ok": True})


@require_admin
async def admin_transactions(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.discord_id, t.points_spent, t.created_at,
                  s.title AS item_title, e.nickname
           FROM transactions t
           LEFT JOIN store_items s ON s.id = t.item_id
           LEFT JOIN employees e ON e.discord_id = t.discord_id
           ORDER BY t.created_at DESC LIMIT 100"""
    ).fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


# --------------------------------------------------------------------------------------
# WEB APP
# --------------------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "index.html")


async def healthcheck(request: web.Request) -> web.StreamResponse:
    return web.json_response({"status": "online", "bot": bool(bot.user and bot.is_ready())})


@web.middleware
async def cors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Allow the standalone Societ-web site to call this API with session cookies."""
    origin = request.headers.get("Origin")
    allowed = origin if origin in ALLOWED_ORIGINS else None

    if request.method == "OPTIONS" and allowed:
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)

    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response


def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.add_routes([
        web.get("/", index),
        web.get("/healthz", healthcheck),

        web.get("/auth/login", auth_login),
        web.get("/auth/callback", auth_callback),
        web.post("/auth/logout", auth_logout),
        web.get("/api/me", api_me),

        web.get("/api/employees", api_employees),
        web.get("/api/games", api_games),
        web.get("/api/store/items", api_store_items),
        web.post("/api/store/redeem", api_store_redeem),
        web.get("/api/me/transactions", api_my_transactions),
        web.patch("/api/me/profile", api_update_profile),

        web.post("/api/admin/employees", admin_upsert_employee),
        web.delete("/api/admin/employees/{emp_id}", admin_delete_employee),
        web.post("/api/admin/points", admin_adjust_points),
        web.post("/api/admin/games", admin_upsert_game),
        web.delete("/api/admin/games/{game_id}", admin_delete_game),
        web.get("/api/admin/store/items", admin_list_store_items),
        web.post("/api/admin/store/items", admin_upsert_store_item),
        web.delete("/api/admin/store/items/{item_id}", admin_delete_store_item),
        web.get("/api/admin/transactions", admin_transactions),
        web.options("/{tail:.*}", lambda request: web.Response(status=204)),
    ])
    app.router.add_static("/assets/", BASE_DIR / "assets")
    return app


async def start_web_server() -> web.AppRunner:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"[Web] Societ portal listening on http://{WEB_HOST}:{WEB_PORT}")
    return runner


# --------------------------------------------------------------------------------------
# DISCORD EVENTS & SLASH COMMANDS
# --------------------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    print("=" * 45)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("=" * 45)
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except discord.DiscordException as exc:
        print(f"Command sync failed: {exc}")


def is_admin_user(discord_id: int) -> bool:
    row = fetch_employee(str(discord_id))
    return bool(row and row["is_admin"])


@bot.tree.command(name="test", description="🩺 Health check for the Societ systems")
async def test(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🛰️ Societ Systems Online",
        description=(
            f"**Bot:** `{bot.user}`\n"
            f"**Latency:** `{round(bot.latency * 1000)} ms`\n"
            f"**Web portal:** `http://{WEB_HOST}:{WEB_PORT}`\n"
            f"**Database:** `{Path(DB_NAME).name}`"
        ),
        color=0x38BDF8,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="work", description="📣 Tell an operative to get back to work in a channel")
@app_commands.describe(channel="Channel that receives the reminder", member="Operative to mention")
async def work(interaction: discord.Interaction, channel: discord.TextChannel,
               member: discord.Member) -> None:
    embed = discord.Embed(
        title="⚙️ Back to work",
        description=f"{member.mention}, please return to your station and submit your work.",
        color=0x34D399,
    )
    await channel.send(content=member.mention, embed=embed)
    await interaction.response.send_message(
        f"✅ Reminder sent to {member.display_name} in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="rd_employee", description="🎲 Pick a random operative (optionally by position)")
@app_commands.describe(channel="Channel that receives the result",
                       position="Filter by position; leave empty to draw from everyone")
async def rd_employee(interaction: discord.Interaction, channel: discord.TextChannel,
                      position: Optional[str] = None) -> None:
    await interaction.response.defer(ephemeral=True)

    conn = get_conn()
    if position and position.strip():
        rows = conn.execute(
            "SELECT * FROM employees WHERE LOWER(position) LIKE LOWER(?)",
            (f"%{position.strip()}%",),
        ).fetchall()
        scope = f"position: {position.strip()}"
    else:
        rows = conn.execute("SELECT * FROM employees").fetchall()
        scope = "all operatives"
    conn.close()

    if not rows:
        await interaction.followup.send(f"❌ No operatives found for {scope}.", ephemeral=True)
        return

    picked = random.choice(rows)
    embed = discord.Embed(
        title=f"🎲 Random operative ({scope})",
        description=(f"<@{picked['discord_id']}> — **{picked['nickname']}**\n"
                     f"Position: **{picked['position']}**"),
        color=0x38BDF8,
    )
    await channel.send(content=f"<@{picked['discord_id']}>", embed=embed)
    await interaction.followup.send(f"✅ Result posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="add_employee", description="🗂️ Add or update an operative record (admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(member="Discord account of the operative", nickname="Display nickname",
                       position="Role inside the studio", bio="Short biography",
                       contact_email="Contact email", points="Starting points balance",
                       is_admin="Grant admin access to the web portal")
async def add_employee(interaction: discord.Interaction, member: discord.Member, nickname: str,
                       position: str, bio: Optional[str] = None,
                       contact_email: Optional[str] = None, points: int = 0,
                       is_admin: bool = False) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO employees (discord_id, nickname, position, bio, contact_email, points, is_admin)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET
               nickname = excluded.nickname,
               position = excluded.position,
               bio = COALESCE(excluded.bio, employees.bio),
               contact_email = COALESCE(excluded.contact_email, employees.contact_email),
               points = excluded.points,
               is_admin = excluded.is_admin""",
        (str(member.id), nickname, position, bio, contact_email, points, int(is_admin)),
    )
    conn.commit()
    conn.close()

    embed = discord.Embed(title="✨ Operative record saved", color=0x34D399)
    embed.add_field(name="Operative", value=member.mention, inline=False)
    embed.add_field(name="Nickname", value=nickname, inline=True)
    embed.add_field(name="Position", value=position, inline=True)
    embed.add_field(name="Points", value=str(points), inline=True)
    embed.add_field(name="Portal admin", value="Yes" if is_admin else "No", inline=True)
    if bio:
        embed.add_field(name="Bio", value=bio, inline=False)
    await interaction.response.send_message(embed=embed)


points_group = app_commands.Group(name="points", description="💠 Employee points system")


@points_group.command(name="check", description="Check the points balance of an operative")
@app_commands.describe(member="Operative to inspect; defaults to yourself")
async def points_check(interaction: discord.Interaction,
                       member: Optional[discord.Member] = None) -> None:
    target = member or interaction.user
    row = fetch_employee(str(target.id))
    if not row:
        await interaction.response.send_message(
            f"❌ {target.mention} is not registered. Use `/add_employee` first.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"💠 Points balance — {row['nickname']}",
        description=f"**{row['points']}** points\nPosition: **{row['position']}**",
        color=0x38BDF8,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@points_group.command(name="give", description="Grant or remove points (admin only)")
@app_commands.describe(member="Operative receiving the adjustment",
                       amount="Points to add (use a negative number to remove)",
                       reason="Why the adjustment happened")
async def points_give(interaction: discord.Interaction, member: discord.Member, amount: int,
                      reason: Optional[str] = None) -> None:
    is_guild_admin = (isinstance(interaction.user, discord.Member)
                      and interaction.user.guild_permissions.administrator)
    if not is_guild_admin and not is_admin_user(interaction.user.id):
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return

    row = fetch_employee(str(member.id))
    if not row:
        await interaction.response.send_message(
            f"❌ {member.mention} is not registered. Use `/add_employee` first.", ephemeral=True)
        return

    conn = get_conn()
    conn.execute("UPDATE employees SET points = MAX(0, points + ?) WHERE discord_id = ?",
                 (amount, str(member.id)))
    conn.commit()
    new_balance = conn.execute("SELECT points FROM employees WHERE discord_id = ?",
                               (str(member.id),)).fetchone()["points"]
    conn.close()

    embed = discord.Embed(
        title="💠 Points updated",
        description=(f"{member.mention} {'received' if amount >= 0 else 'lost'} "
                     f"**{abs(amount)}** points.\nNew balance: **{new_balance}**"),
        color=0x34D399 if amount >= 0 else 0xF87171,
    )
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(points_group)


profile_group = app_commands.Group(name="profile", description="🪪 Manage your own operative profile")


@profile_group.command(name="bio", description="Update your own biography")
@app_commands.describe(text="The biography shown on your roster card")
async def profile_bio(interaction: discord.Interaction, text: str) -> None:
    if not fetch_employee(str(interaction.user.id)):
        await interaction.response.send_message(
            "❌ You are not registered yet. Ask an admin to run `/add_employee`.", ephemeral=True)
        return

    conn = get_conn()
    conn.execute("UPDATE employees SET bio = ? WHERE discord_id = ?", (text, str(interaction.user.id)))
    conn.commit()
    conn.close()
    await interaction.response.send_message("✅ Biography updated.", ephemeral=True)


@profile_group.command(name="visibility", description="Show or hide your card on the public roster")
@app_commands.describe(visible="True shows your profile publicly, False hides it")
async def profile_visibility(interaction: discord.Interaction, visible: bool) -> None:
    if not fetch_employee(str(interaction.user.id)):
        await interaction.response.send_message(
            "❌ You are not registered yet. Ask an admin to run `/add_employee`.", ephemeral=True)
        return

    conn = get_conn()
    conn.execute("UPDATE employees SET is_visible = ? WHERE discord_id = ?",
                 (int(visible), str(interaction.user.id)))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"✅ Your profile is now **{'visible' if visible else 'hidden'}** on the public roster.",
        ephemeral=True)


bot.tree.add_command(profile_group)


# --------------------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------------------

async def main() -> None:
    init_db()
    runner = await start_web_server()
    try:
        if not TOKEN:
            print("[Bot] DISCORD_TOKEN is not set — running the web portal only.")
            await asyncio.Event().wait()
        else:
            async with bot:
                await bot.start(TOKEN)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
    except discord.LoginFailure:
        print("Invalid bot token. Check your DISCORD_TOKEN environment variable.")
