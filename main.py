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
import numpy as np 
import datetime
from zoneinfo import ZoneInfo
from discord.ext import commands, tasks
from discord import app_commands



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

DISCORD_API = "https://discord.com/api/v10"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True


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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            meeting_time TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            mention_id INTEGER NOT NULL,
            is_done INTEGER DEFAULT 0
        )
    """)

    # Inside init_db()
    cursor.execute("""
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
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            log_channel_id INTEGER,
            voice_master_id INTEGER,
            voice_category_id INTEGER
        )
    """)
    # ตารางเก็บข้อมูลห้องตั๋ว (Ticket) ที่กำลังเปิดอยู่
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_channel_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            status TEXT DEFAULT 'open'
        )
    """)

    conn.commit()
    conn.close()
    print("[Database] Tables ready in", DB_NAME)


@bot.event
async def on_member_join(member):
    """เมื่อมีคนเข้าเซิร์ฟเวอร์"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        channel = member.guild.get_channel(row[0])
        if channel:
            embed = discord.Embed(
                title=f"🎉 ยินดีต้อนรับ! 🎉",
                description=f"ยินดีต้อนรับ {member.mention} เข้าทำงาน!\nตั้งใจทำงานนะ!",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

@bot.event
async def on_message_delete(message):
    """Log เมื่อมีคนลบข้อความ"""
    if message.author.bot: return
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (message.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        log_channel = message.guild.get_channel(row[0])
        if log_channel:
            embed = discord.Embed(title="🗑️ ข้อความถูกลบ", color=discord.Color.red(), timestamp=message.created_at)
            embed.add_field(name="คนพิมพ์", value=message.author.mention)
            embed.add_field(name="ช่อง", value=message.channel.mention)
            embed.add_field(name="ข้อความที่ลบ", value=message.content or "[ไม่มีข้อความตัวอักษร]", inline=False)
            await log_channel.send(embed=embed)


@bot.event
async def on_voice_state_update(member, before, after):
    """ตรวจจับการเข้า-ออกห้องเสียง"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT voice_master_id, voice_category_id FROM settings WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if not row: return
    master_id, category_id = row

    # กรณีที่ 1: สมาชิกกดเข้า "ห้องสร้างห้องหลัก"
    if after.channel and after.channel.id == master_id:
        category = member.guild.get_channel(category_id) if category_id else None

        # สร้างห้องเสียงใหม่ โดยตั้งชื่อตามคนกดเข้า
        new_channel = await member.guild.create_voice_channel(
            name=f"💻│ ห้องทำงานของ {member.display_name}",
            category=category
        )
        # ย้ายสมาชิกลงห้องใหม่ทันที
        await member.move_to(new_channel)

    # กรณีที่ 2: สมาชิกย้ายออกหรือวางสาย เช็คว่าห้องชั่วคราวว่างไหม ถ้าว่างให้ลบทิ้ง
    if before.channel and before.channel.id != master_id:
        # เพิ่มการเช็คชื่อห้องก่อนลบ: ต้องอยู่ใน Category เดียวกัน, ชื่อขึ้นต้นด้วย '💻│' และไม่มีคนอยู่
        if before.channel.category_id == category_id and before.channel.name.startswith("💻│") and len(before.channel.members) == 0:
            try:
                await before.channel.delete()
            except Exception as e:
                print(f"❌ ไม่สามารถลบห้องเสียงได้: {e}")

class TicketPersistentView(discord.ui.View):
    """สร้างปุ่มเปิดตั๋วถาวร"""
    def __init__(self):
        super().__init__(timeout=None) 

    @discord.ui.button(label="📩 เปิด Ticket แจ้งปัญหา", style=discord.ButtonStyle.primary, custom_id="press_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        await interaction.response.defer(ephemeral=True)

        # ตั้งสิทธิ์ห้องแชทลับ (แอดมินเห็น, คนเปิดเห็น, คนอื่นห้ามเห็น)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{(user.name).lower()}",
            overwrites=overwrites
        )

        # บันทึกข้อมูลลงฐานข้อมูล
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tickets (ticket_channel_id, user_id) VALUES (?, ?)", (ticket_channel.id, user.id))
        conn.commit()
        conn.close()

        # ส่งข้อความควบคุมในตั๋วพร้อมปุ่มปิด
        embed = discord.Embed(
            title="🎫 Ticket ติดต่อแอดมิน",
            description=f" {user.mention} พิมพ์รายละเอียดปัญหาหรือเรื่องที่ต้องการสอบถามไว้ได้เลย\nแอดมินจะมาตรวจสอบให้ในไม่ช้า",
            color=discord.Color.blue()
        )
        await ticket_channel.send(embed=embed, view=TicketCloseView())
        await interaction.followup.send(f"✅ เปิดตั๋วเรียบร้อยแล้วที่ห้อง {ticket_channel.mention} ", ephemeral=True)

class TicketCloseView(discord.ui.View):
    """ปุ่มสำหรับกดปิดตั๋วในห้องคุยลับ"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 ปิด Ticket", style=discord.ButtonStyle.danger, custom_id="press_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("กำลังปิดและลบห้องนี้ใน 5 วินาที...")

        # ลบข้อมูลออกจากดาต้าเบส
        conn = sqlite3.connect(DB_NAME)
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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # ค้นหาการประชุมที่ถึงเวลาแล้วและยังไม่ได้ทำการแจ้งเตือน (is_done = 0)
    cursor.execute(
        "SELECT id, topic, channel_id, mention_id FROM schedules WHERE meeting_time <= ? AND is_done = 0", 
        (now_str,)
    )
    meetings = cursor.fetchall()

    for meeting in meetings:
        db_id, topic, channel_id, mention_id = meeting
        channel = bot.get_channel(channel_id)

        if channel:
            alert_embed = discord.Embed(
                title="🚨 ได้เวลาประชุมแล้ว! 🚨",
                description=f"ขณะนี้ถึงเวลานัดหมายการประชุมที่บันทึกไว้ในระบบแล้ว\n\n**📌 หัวข้อการประชุม:** {topic}",
                color=discord.Color.red(),
                timestamp=datetime.datetime.now()
            )

            # [แก้ไขจุดที่ 3] ดึงข้อมูล Role จากกิลด์ของแชนแนลโดยตรง ป้องกันข้อผิดพลาดในเคสที่บอทยังโหลดข้อมูลกิลด์สากลไม่เสร็จ
            mention_text = f"<@&{mention_id}>" if channel.guild.get_role(mention_id) else f"<@{mention_id}>"

            try:
                await channel.send(content=mention_text, embed=alert_embed)
            except Exception as e:
                print(f"❌ ไม่สามารถส่งข้อความแจ้งเตือนได้: {e}")

        # อัปเดตสถานะในฐานข้อมูลว่าแจ้งเตือนแล้ว เพื่อไม่ให้บอทส่งซ้ำ
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
    conn = get_conn()
    rows = conn.execute("SELECT * FROM employees ORDER BY is_admin DESC, nickname ASC").fetchall()
    conn.close()
    return web.json_response([employee_public(row) for row in rows])


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
# WEB APP
# --------------------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "index.html")


async def healthcheck(request: web.Request) -> web.StreamResponse:
    return web.json_response({"status": "online", "bot": bool(bot.user and bot.is_ready())})


def build_app() -> web.Application:
    app = web.Application()
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


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="test_welcome", description="🧪 ทดสอบส่งการ์ดต้อนรับสมาชิกใหม่")
async def test_welcome(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        channel = interaction.guild.get_channel(row[0])
        if channel:
            embed = discord.Embed(
                title="🎉 [TEST] ยินดีต้อนรับ! 🎉", 
                description=f"ยินดีต้อนรับ {interaction.user.mention} เข้าทำงาน!\nตั้งใจทำงานนะ!", 
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await channel.send(embed=embed)
            await interaction.followup.send("✅ ส่งข้อความทดสอบต้อนรับไปที่ห้องแล้ว!", ephemeral=True)
        else:
            await interaction.followup.send("❌ หาห้องต้อนรับไม่เจอ (ห้องนั้นอาจถูกลบไปแล้ว)", ephemeral=True)
    else:
        await interaction.followup.send("❌ คุณยังไม่ได้ตั้งค่าห้องต้อนรับ กรุณาใช้ `/setup_systems` เพื่อตั้งค่าก่อน", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="test_log", description="🧪 ทดสอบส่ง Log ข้อความถูกลบ (เข้าห้องที่ตั้งค่าไว้)")
async def test_log(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        log_channel = interaction.guild.get_channel(row[0])
        if log_channel:
            embed = discord.Embed(title="🗑️ [TEST LOG] ข้อความถูกลบ", color=discord.Color.red(), timestamp=interaction.created_at)
            embed.add_field(name="คนพิมพ์", value=interaction.user.mention)
            embed.add_field(name="ช่อง", value=interaction.channel.mention)
            embed.add_field(name="ข้อความที่ลบ", value="นี่คือข้อความสมมุติสำหรับทดสอบระบบ Log", inline=False)
            await log_channel.send(embed=embed)
            await interaction.followup.send("✅ ส่ง Log ทดสอบไปที่ห้องแล้ว!", ephemeral=True)
        else:
            await interaction.followup.send("❌ หาห้อง Log ไม่เจอ (อาจจะลบห้องนั้นไปแล้ว)", ephemeral=True)
    else:
        await interaction.followup.send("❌ ยังไม่ได้ตั้งค่าห้อง Log ใช้ `/setup_systems` ตั้งอัปเดตก่อน", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="setup_systems", description="ตั้งค่าห้องต่าง ๆ ของทั้ง 3 ระบบ")
async def setup_systems(
    interaction: discord.Interaction,
    welcome_channel: discord.TextChannel = None,
    log_channel: discord.TextChannel = None,
    voice_master_channel: discord.VoiceChannel = None,
    voice_category: discord.CategoryChannel = None
):
    guild_id = interaction.guild.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT guild_id FROM settings WHERE guild_id = ?", (guild_id,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO settings (guild_id) VALUES (?)", (guild_id,))
        conn.commit()

    if welcome_channel:
        cursor.execute("UPDATE settings SET welcome_channel_id = ? WHERE guild_id = ?", (welcome_channel.id, guild_id))
    if log_channel:
        cursor.execute("UPDATE settings SET log_channel_id = ? WHERE guild_id = ?", (log_channel.id, guild_id))
    if voice_master_channel:
        cursor.execute("UPDATE settings SET voice_master_id = ? WHERE guild_id = ?", (voice_master_channel.id, guild_id))
    if voice_category:
        cursor.execute("UPDATE settings SET voice_category_id = ? WHERE guild_id = ?", (voice_category.id, guild_id))

    conn.commit()
    conn.close()
    await interaction.response.send_message("⚙️ อัปเดตการตั้งค่าระบบแล้ว!", ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="send_ticket_button", description="ส่งปุ่มกดสร้าง Ticket")
async def send_ticket_button(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📞 Support Ticket",
        description="แจ้งปัญหากับ director หรือถามเกี่ยวกับงานของตัวเอง",
        color=discord.Color.gold()
    )
    await interaction.response.send_message("ส่งแผงควบคุมสำเร็จ", ephemeral=True)
    await interaction.channel.send(embed=embed, view=TicketPersistentView())


@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="meeting", description="สร้างนัดหมายการประชุมและบันทึกลงฐานข้อมูล")
@app_commands.describe(
    topic="หัวข้อการประชุม",
    date_str="วันที่ประชุม (รูปแบบ วว/ดด/ปปปป เช่น 25/06/2026)",
    time_str="เวลาประชุม (รูปแบบ ชช:นน เช่น 14:30)",
    channel="ช่องที่ต้องการให้ส่งข้อความแจ้งเตือน",
    mention_target="บทบาท (Role) หรือผู้ใช้ที่จะแท็กเข้าประชุม"
)
async def meeting(
    interaction: discord.Interaction, 
    topic: str, 
    date_str: str, 
    time_str: str, 
    channel: discord.TextChannel, 
    mention_target: discord.Role | discord.Member
):
    try:
        input_time = datetime.datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
        now = datetime.datetime.now(ZoneInfo("Asia/Bangkok")).replace(tzinfo=None)

        if input_time <= now:
            await interaction.response.send_message("❌ ไม่สามารถนัดหมายเวลาในอดีตได้ครับ กรุณาระบุเวลาใหม่", ephemeral=True)
            return

        save_time_str = input_time.strftime("%Y-%m-%d %H:%M")

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO schedules (topic, meeting_time, channel_id, mention_id) VALUES (?, ?, ?, ?)",
            (topic, save_time_str, channel.id, mention_target.id)
        )
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title="💾 บันทึกการนัดหมายประชุมสำเร็จ", 
            color=discord.Color.green(),
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="📌 หัวข้อ", value=topic, inline=False)
        embed.add_field(name="📆 วันเวลา", value=f"{date_str} เวลา {time_str} น.", inline=True)
        embed.add_field(name="📢 ช่องแจ้งเตือน", value=channel.mention, inline=True)
        embed.add_field(name="👥 ผู้เข้าร่วม", value=mention_target.mention, inline=False)
        embed.set_footer(text="ระบบบันทึกลงข้อมูลระยะยาวเรียบร้อย")

        await interaction.response.send_message(embed=embed)

    except ValueError:
        await interaction.response.send_message(
            "❌ กรอกรูปแบบวันเวลาผิด! ตัวอย่างที่ถูกต้อง: วันที่ `25/06/2026` และ เวลา `14:30`", 
            ephemeral=True
        )


@bot.tree.command(name="meeting_list", description="ดูรายการนัดหมายประชุมทั้งหมด")
async def meeting_list(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, topic, meeting_time FROM schedules WHERE is_done = 0")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📅 ไม่มีนัดหมายที่ค้างอยู่", ephemeral=True)
        return

    embed = discord.Embed(title="📋 รายการนัดหมายทั้งหมด", color=discord.Color.orange())
    for row in rows:
        db_id, topic, m_time = row
        embed.add_field(
            name=f"🆔 ID: {db_id} | {topic}", 
            value=f"⏰ เวลา: {m_time}", 
            inline=False
        )
    await interaction.response.send_message(embed=embed)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="meeting_delete", description="ลบนัดหมายที่ทำผิด โดยใช้ ID")
@app_commands.describe(db_id="เลข ID ของนัดหมายที่ต้องการลบ (ดูได้จาก /meeting_list)")
async def meeting_delete(interaction: discord.Interaction, db_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT topic FROM schedules WHERE id = ? AND is_done = 0", (db_id,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message(f"❌ ไม่พบนัดหมายรหัส ID: {db_id} หรือนัดหมายนั้นทำงานไปแล้ว", ephemeral=True)
        conn.close()
        return

    cursor.execute("DELETE FROM schedules WHERE id = ?", (db_id,))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"🗑️ ลบนัดหมาย ID: {db_id} ({row[0]}) เรียบร้อยแล้วครับ!")


@bot.tree.command(name="employee_add", description="Add or update an employee in the system")
@app_commands.describe(
    member="The Discord member",
    emp_id="Employee ID (e.g., EMP-001)",
    nickname="Employee's nickname",
    position="Job position",
    mbti="MBTI Personality (Optional)",
    is_admin="Is this user a portal admin? (True/False)"
)
async def employee_add(
    interaction: discord.Interaction, 
    member: discord.Member, 
    emp_id: str, 
    nickname: str, 
    position: str, 
    mbti: str = "Unknown",
    is_admin: bool = False
):
    # Ensure only admins can use this command (Optional but recommended)
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    conn = get_conn()
    
    # 1. Upsert into database
    conn.execute(
        """INSERT INTO employees (discord_id, emp_id, nickname, position, mbti, is_admin, points)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET
               emp_id = excluded.emp_id,
               nickname = excluded.nickname,
               position = excluded.position,
               mbti = excluded.mbti,
               is_admin = excluded.is_admin""",
        (str(member.id), emp_id, nickname, position, mbti, int(is_admin), 0)
    )
    conn.commit()
    
    # Fetch current points in case they already existed and had points
    row = conn.execute("SELECT points FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    current_points = row["points"] if row else 0
    conn.close()

    # 2. Build the Embed
    embed = discord.Embed(
        title="✅ Employee Profile Updated", 
        description=f"Successfully updated the database for {member.mention}",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    
    embed.add_field(name="Emp ID", value=emp_id, inline=True)
    embed.add_field(name="Nickname", value=nickname, inline=True)
    embed.add_field(name="Position", value=position, inline=True)
    
    if mbti != "Unknown":
        embed.add_field(name="MBTI", value=mbti, inline=True)
        
    embed.add_field(name="Points", value=str(current_points), inline=True)
    
    # Using the inline formatting logic you requested
    embed.add_field(name="Portal admin", value="Yes" if is_admin else "No", inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="list_employees", description="ดูรายชื่อและข้อมูลพนักงานทั้งหมดในระบบ")
async def list_employees(interaction: discord.Interaction):
    conn = get_conn()
    rows = conn.execute("SELECT discord_id, emp_id, nickname, position, mbti FROM employees").fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📭 ยังไม่มีข้อมูลพนักงานในระบบ", ephemeral=True)
        return

    embed = discord.Embed(title="👥 รายชื่อพนักงานทั้งหมดในระบบ", color=discord.Color.purple())

    for row in rows:
        # ใช้ key access เพราะ get_conn() คืนค่าเป็น sqlite3.Row
        discord_id = row["discord_id"]
        emp_id = row["emp_id"] or "N/A"
        nickname = row["nickname"]
        position = row["position"]
        mbti = row["mbti"] or "N/A"
        
        embed.add_field(
            name=f"⭐ [{emp_id}] {nickname}",
            value=f"**ตำแหน่ง:** {position} | **MBTI:** {mbti}\n**บัญชี Discord:** <@{discord_id}>",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="view_employee", description="ดูข้อมูลโปรไฟล์ของพนักงานที่ระบุ")
@app_commands.describe(member="เลือกพนักงานที่ต้องการดูข้อมูล")
async def view_employee(interaction: discord.Interaction, member: discord.Member):
    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message(f"❌ ไม่พบข้อมูลของ {member.mention} ในระบบ", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 โปรไฟล์พนักงาน: {row['nickname']}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    
    embed.add_field(name="รหัสพนักงาน", value=row["emp_id"] or "N/A", inline=True)
    embed.add_field(name="ชื่อเล่น", value=row["nickname"], inline=True)
    embed.add_field(name="ตำแหน่ง", value=row["position"], inline=True)
    embed.add_field(name="MBTI", value=row["mbti"] or "N/A", inline=True)
    embed.add_field(name="คะแนนสะสม (Points)", value=str(row["points"]), inline=True)
    embed.add_field(name="ผู้ดูแลระบบ (Admin)", value="Yes" if row["is_admin"] else "No", inline=True)

    if row["bio"]:
        embed.add_field(name="ประวัติ (Bio)", value=row["bio"], inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="delete_employee", description="ลบพนักงานออกจากระบบ (เฉพาะ Admin)")
@app_commands.describe(member="เลือกพนักงานที่ต้องการลบ")
async def delete_employee(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้", ephemeral=True)
        return

    conn = get_conn()
    row = conn.execute("SELECT nickname FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    
    if not row:
        conn.close()
        await interaction.response.send_message(f"❌ ไม่พบข้อมูลของ {member.mention} ในระบบ", ephemeral=True)
        return

    conn.execute("DELETE FROM employees WHERE discord_id = ?", (str(member.id),))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"🗑️ ลบข้อมูลพนักงานของ {member.mention} ออกจากระบบเรียบร้อยแล้ว")


@bot.tree.command(name="rd_employee", description="สุ่มเลือกพนักงาน 1 คนจากระบบ")
async def rd_employee(interaction: discord.Interaction):
    conn = get_conn()
    # ใช้ ORDER BY RANDOM() ของ SQLite เพื่อสุ่ม
    row = conn.execute("SELECT * FROM employees ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message("📭 ยังไม่มีข้อมูลพนักงานในระบบให้สุ่ม", ephemeral=True)
        return

    discord_id = row["discord_id"]
    emp_id = row["emp_id"] or "N/A"
    
    embed = discord.Embed(
        title="🎲 สุ่มพนักงานผู้โชคดี!",
        description=f"ยินดีด้วย! <@{discord_id}> ได้รับการสุ่มเลือก",
        color=discord.Color.gold()
    )
    embed.add_field(name="รหัสพนักงาน", value=emp_id, inline=True)
    embed.add_field(name="ชื่อเล่น", value=row["nickname"], inline=True)
    embed.add_field(name="ตำแหน่ง", value=row["position"], inline=True)

    await interaction.response.send_message(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

@bot.tree.command(name="hello", description="Say hello!")
async def hello(interaction: discord.Interaction):
    try:
        await interaction.response.send_message(f"hello {interaction.user.mention}! 👋", ephemeral=False)
    except Exception as e:
        await interaction.response.send_message("An error occurred.", ephemeral=True)
        print(f"Slash command error: {e}")



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
