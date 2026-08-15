import asyncio
import os
import discord
import aiosqlite
from aiohttp import web
import aiohttp_jinja2
import jinja2
from aiohttp_session import setup as setup_session, get_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import aiohttp
from dotenv import load_dotenv
import base64
import datetime
import hashlib
import hmac
import json
import random
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp
from discord import app_commands
from discord.ext import commands, tasks

load_dotenv()

# ================= Configs =================
BASE_DIR = Path(__file__).resolve().parent
DB_NAME = os.getenv("SOCIET_DB", str(BASE_DIR / "database.db"))

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:13660/auth/callback")
ADMIN_WEBHOOK_URL = os.getenv("ADMIN_WEBHOOK_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
SESSION_COOKIE = "societ_session"
SESSION_TTL = 60 * 60 * 24 * 7
SESSION_SECURE = os.getenv("SESSION_SECURE", "false").lower() == "true"
ADMIN_ROLE_ID = 1511341249985122485

WEB_HOST = os.getenv("HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "13660"))

DISCORD_API = "https://discord.com/api/v10"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True




class SocietBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # ลงทะเบียน Persistent Views เพื่อให้ปุ่มใช้งานได้ถาวร
        self.add_view(TicketPersistentView())
        self.add_view(TicketCloseView())
        # เริ่มการทำงานของ Background Task
        if not check_meetings.is_running():
            check_meetings.start()

# ================= Database =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        
        await db.execute("""
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
        await db.execute("""
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT UNIQUE NOT NULL,
                emp_id TEXT,
                nickname TEXT NOT NULL,
                position TEXT NOT NULL,
                mbti TEXT,
                bio TEXT,
                contact_email TEXT,
                points INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                mention_id INTEGER NOT NULL,
                is_done INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                log_channel_id INTEGER,
                voice_master_id INTEGER,
                voice_category_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_channel_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                status TEXT DEFAULT 'open'
            )
        """)
        await db.commit()
    print("[Database] Tables ready in", DB_NAME)




# --------------------------------------------------------------------------------------
# DISCORD EVENTS
# --------------------------------------------------------------------------------------

@bot.event
async def on_member_join(member):
    """เมื่อมีคนเข้าเซิร์ฟเวอร์"""
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["welcome_channel_id"]:
        channel = member.guild.get_channel(row["welcome_channel_id"])
        if channel:
            embed = discord.Embed(
                title="🎉 ยินดีต้อนรับ! 🎉",
                description=f"ยินดีต้อนรับ {member.mention} เข้าทำงาน!\nตั้งใจทำงานนะ!",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)


@bot.event
async def on_message_delete(message):
    """Log เมื่อมีคนลบข้อความ"""
    if message.author.bot or not message.guild:
        return
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (message.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["log_channel_id"]:
        log_channel = message.guild.get_channel(row["log_channel_id"])
        if log_channel:
            embed = discord.Embed(title="🗑️ ข้อความถูกลบ", color=discord.Color.red(), timestamp=message.created_at)
            embed.add_field(name="คนพิมพ์", value=message.author.mention)
            embed.add_field(name="ช่อง", value=message.channel.mention)
            embed.add_field(name="ข้อความที่ลบ", value=message.content or "[ไม่มีข้อความตัวอักษร]", inline=False)
            await log_channel.send(embed=embed)


@bot.event
async def on_voice_state_update(member, before, after):
    """ตรวจจับการเข้า-ออกห้องเสียง"""
    if not member.guild:
        return
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT voice_master_id, voice_category_id FROM settings WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return
    master_id, category_id = row["voice_master_id"], row["voice_category_id"]

    if after.channel and after.channel.id == master_id:
        category = member.guild.get_channel(category_id) if category_id else None

        new_channel = await member.guild.create_voice_channel(
            name=f"💻│ ห้องทำงานของ {member.display_name}",
            category=category
        )
        await member.move_to(new_channel)

    if before.channel and before.channel.id != master_id:
        if before.channel.category_id == category_id and before.channel.name.startswith("💻│") and len(before.channel.members) == 0:
            try:
                await before.channel.delete()
            except Exception as e:
                print(f"❌ ไม่สามารถลบห้องเสียงได้: {e}")


# --------------------------------------------------------------------------------------
# TICKET VIEWS
# --------------------------------------------------------------------------------------

class TicketPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 

    @discord.ui.button(label="📩 เปิด Ticket แจ้งปัญหา", style=discord.ButtonStyle.primary, custom_id="press_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        await interaction.response.defer(ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{(user.name).lower()}",
            overwrites=overwrites
        )

        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tickets (ticket_channel_id, user_id) VALUES (?, ?)", (ticket_channel.id, user.id))
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="🎫 Ticket ติดต่อแอดมิน",
            description=f"{user.mention} พิมพ์รายละเอียดปัญหาหรือเรื่องที่ต้องการสอบถามไว้ได้เลย\nแอดมินจะมาตรวจสอบให้ในไม่ช้า",
            color=discord.Color.blue()
        )
        await ticket_channel.send(embed=embed, view=TicketCloseView())
        await interaction.followup.send(f"✅ เปิดตั๋วเรียบร้อยแล้วที่ห้อง {ticket_channel.mention}", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 ปิด Ticket", style=discord.ButtonStyle.danger, custom_id="press_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("กำลังปิดและลบห้องนี้ใน 5 วินาที...")

        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tickets WHERE ticket_channel_id = ?", (interaction.channel.id,))
        conn.commit()
        conn.close()

        await asyncio.sleep(5)
        await interaction.channel.delete()


@tasks.loop(seconds=30)
async def check_meetings():
    now = datetime.datetime.now()
    now_tz = now.astimezone(ZoneInfo("Asia/Bangkok"))
    now_str = now_tz.strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, topic, channel_id, mention_id FROM schedules WHERE meeting_time <= ? AND is_done = 0", 
        (now_str,)
    )
    meetings = cursor.fetchall()

    for meeting in meetings:
        db_id, topic, channel_id, mention_id = meeting["id"], meeting["topic"], meeting["channel_id"], meeting["mention_id"]
        channel = bot.get_channel(channel_id)

        if channel:
            alert_embed = discord.Embed(
                title="🚨 ได้เวลาประชุมแล้ว! 🚨",
                description=f"ขณะนี้ถึงเวลานัดหมายการประชุมที่บันทึกไว้ในระบบแล้ว\n\n**📌 หัวข้อการประชุม:** {topic}",
                color=discord.Color.red(),
                timestamp=datetime.datetime.now()
            )

            mention_text = f"<@&{mention_id}>" if channel.guild.get_role(mention_id) else f"<@{mention_id}>"

            try:
                await channel.send(content=mention_text, embed=alert_embed)
            except Exception as e:
                print(f"❌ ไม่สามารถส่งข้อความแจ้งเตือนได้: {e}")

        cursor.execute("UPDATE schedules SET is_done = 1 WHERE id = ?", (db_id,))

    conn.commit()
    conn.close()


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
    }


# --------------------------------------------------------------------------------------
# SESSION HANDLING
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
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if not payload:
        return None
    row = fetch_employee(payload["discord_id"])
    if not row:
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
    response.set_cookie(
        "societ_oauth_state", 
        state, 
        max_age=600, 
        httponly=True, 
        samesite="Lax",
        secure=SESSION_SECURE
    )
    raise response


async def auth_callback(request: web.Request) -> web.StreamResponse:
    code = request.query.get("code")
    state = request.query.get("state")
    cookie_state = request.cookies.get("societ_oauth_state")

    if not code:
        return web.json_response({"error": "missing_code"}, status=400)

    if cookie_state and state != cookie_state:
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
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(payload),
        max_age=SESSION_TTL,
        path="/",
        httponly=True,
        samesite="Lax",
        secure=SESSION_SECURE
    )
    if "societ_oauth_state" in request.cookies:
        response.del_cookie("societ_oauth_state", path="/")
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
# REST API (EMPLOYEE & PUBLIC)
# --------------------------------------------------------------------------------------

async def api_employees(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM employees ORDER BY is_admin DESC, nickname ASC").fetchall()
    conn.close()
    return web.json_response([employee_public(row) for row in rows])


async def api_games(request: web.Request) -> web.StreamResponse:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM games ORDER BY created_at DESC, id DESC").fetchall()
    conn.close()
    return web.json_response([dict(row) for row in rows])


@require_employee
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
    body = await read_json(request)
    fields = {key: body[key] for key in ("nickname", "position", "bio", "contact_email")
              if key in body and body[key] is not None}
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
        """INSERT INTO employees (discord_id, emp_id, nickname, position, mbti, bio, contact_email, points, is_admin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET
               emp_id = excluded.emp_id,
               nickname = excluded.nickname,
               position = excluded.position,
               mbti = excluded.mbti,
               bio = excluded.bio,
               contact_email = excluded.contact_email,
               points = excluded.points,
               is_admin = excluded.is_admin""",
        (
            discord_id, 
            body.get("emp_id"), 
            nickname, 
            position, 
            body.get("mbti"), 
            body.get("bio"), 
            body.get("contact_email"),
            int(body.get("points") or 0), 
            int(bool(body.get("is_admin")))
        ),
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
# WEB APP SETUP
# --------------------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "index.html")


async def healthcheck(request: web.Request) -> web.StreamResponse:
    return web.json_response({"status": "online", "bot": bool(bot.user and bot.is_ready())})


# 1. เขียน Handlers (ห้องเป้าหมาย)
async def page_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "pages" / "index.html")

async def page_operatives(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "pages" / "operatives.html")

async def page_archives(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "pages" / "archives.html")

@require_employee
async def page_store(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "pages" / "store.html")

@require_admin
async def page_admin(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "pages" / "admin.html")


# 2. นำมาผูก URL ใน build_app() (ป้ายบอกทาง)
def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        # --- หน้าเว็บ MPA Pages ---
        web.get("/", page_index),                    # พิมพ์ / ให้วิ่งไป page_index
        web.get("/operatives", page_operatives),    # พิมพ์ /operatives ให้วิ่งไป page_operatives
        web.get("/archives", page_archives),        # พิมพ์ /archives ให้วิ่งไป page_archives
        web.get("/store", page_store),              # พิมพ์ /store ให้วิ่งไป page_store
        web.get("/admin", page_admin),              # พิมพ์ /admin ให้วิ่งไป page_admin

        # --- ระบบ Auth & REST APIs เดิมของคุณ ---
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
    ])

    assets_dir = BASE_DIR / "assets"
    if assets_dir.exists():
        app.router.add_static("/assets/", assets_dir)
        
    return app


async def start_web_server() -> web.AppRunner:
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"[Web] Societ portal listening on http://{WEB_HOST}:{WEB_PORT}")
    return runner




# ================= Web Routes =================
routes = web.RouteTableDef()

async def check_admin(user_id: int, bot: commands.Bot) -> bool:
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return False
    member = guild.get_member(user_id)
    if not member:
        try:
            member = await guild.fetch_member(user_id)
        except:
            return False
    return any(role.id == ADMIN_ROLE_ID for role in member.roles)

@routes.get("/")
@aiohttp_jinja2.template("index.html")
async def web_index(request):
    session = await get_session(request)
    return {"user": session.get("user")}

@routes.get("/operatives")
@aiohttp_jinja2.template("operatives.html")
async def web_operatives(request):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM employees") as cursor:
            employees = await cursor.fetchall()
    return {"employees": employees}

@routes.get("/archives")
@aiohttp_jinja2.template("archives.html")
async def web_archives(request):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM games") as cursor:
            games = await cursor.fetchall()
    return {"games": games}

@routes.get("/store")
@aiohttp_jinja2.template("store.html")
async def web_store(request):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM store_items WHERE is_active = 1") as cursor:
            items = await cursor.fetchall()
    return {"items": items}

@routes.get("/admin")
@aiohttp_jinja2.template("admin.html")
async def web_admin(request):
    session = await get_session(request)
    user = session.get("user")
    if not user:
        raise web.HTTPFound("/login")
    
    # Check Role
    is_admin = await check_admin(int(user['id']), request.app['bot'])
    if not is_admin:
        return web.Response(text="Access Denied: You do not have the Admin Role.", status=403)
    
    return {"user": user}

@routes.get("/login")
async def login(request):
    discord_auth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    raise web.HTTPFound(discord_auth_url)

@routes.get("/callback")
async def callback(request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="No code provided")
    
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    async with aiohttp.ClientSession() as http_session:
        # Get Token
        async with http_session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
            token_info = await resp.json()
            if "access_token" not in token_info:
                return web.Response(text="Failed to get token")
            access_token = token_info["access_token"]
        
        # Get User Info
        headers = {"Authorization": f"Bearer {access_token}"}
        async with http_session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            user_info = await resp.json()
            
    session = await get_session(request)
    session["user"] = {"id": user_info["id"], "username": user_info["username"]}
    raise web.HTTPFound("/admin")

@routes.get("/logout")
async def logout(request):
    session = await get_session(request)
    session.invalidate()
    raise web.HTTPFound("/")

# ================= Discord Bot =================
class SocietBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        
    async def setup_hook(self):
        await init_db()
        await self.tree.sync()
        
        # Web App Setup
        app = web.Application()
        app['bot'] = self
        setup_session(app, EncryptedCookieStorage(SECRET_KEY))
        aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))
        app.router.add_static('/static/', path='static', name='static')
        app.add_routes(routes)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"[Web] Started on port {PORT}")

bot = SocietBot()

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user}")

# --- Bot Commands (โครงสร้างพื้นฐานให้ใช้งานได้) ---
@bot.tree.command(name="add_employee", description="Add an employee to the database")
async def add_employee(interaction: discord.Interaction, member: discord.Member, nickname: str, position: str):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO employees (discord_id, nickname, position) VALUES (?, ?, ?)",
                (str(member.id), nickname, position)
            )
            await db.commit()
            await interaction.response.send_message(f"Added {nickname} ({position}) to database.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.tree.command(name="list_employees", description="List all employees")
async def list_employees(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT nickname, position FROM employees") as cursor:
            rows = await cursor.fetchall()
            if not rows:
                await interaction.response.send_message("No employees found.")
                return
            msg = "\n".join([f"- {r[0]} ({r[1]})" for r in rows])
            await interaction.response.send_message(f"**Employees:**\n{msg}")

@bot.tree.command(name="work", description="Work to earn points")
async def work(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_NAME) as db:
        # สมมติว่าได้ 10 point ต่อการ work 1 ครั้ง
        await db.execute("UPDATE employees SET points = points + 10 WHERE discord_id = ?", (str(interaction.user.id),))
        if db.total_changes > 0:
            await db.commit()
            await interaction.response.send_message("You worked and earned 10 points!")
        else:
            await interaction.response.send_message("You are not registered as an employee. Contact Admin.")

# สร้าง Command อื่นๆ เป็น Skeleton ไว้เติมเนื้อหาได้เลย
commands_list = ["test_welcome", "test_log", "setup_systems", "send_ticket_button", 
                 "meeting", "meeting_list", "meeting_delete", "view_employee", 
                 "delete_employee", "rd_employee", "points_give"]

def create_command(cmd_name):
    @bot.tree.command(name=cmd_name, description=f"Execute {cmd_name}")
    async def cmd(interaction: discord.Interaction):
        await interaction.response.send_message(f"Command `/{cmd_name}` is ready to be programmed!")
    return cmd

for cmd in commands_list:
    create_command(cmd)

if __name__ == "__main__":
    bot.run(TOKEN)
