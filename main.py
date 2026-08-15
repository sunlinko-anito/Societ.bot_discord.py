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
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import typing
from discord import app_commands
from discord.ext import commands, tasks

load_dotenv()

# ================= Configs =================
BASE_DIR = Path(__file__).resolve().parent
DB_NAME = os.getenv("SOCIET_DB", str(BASE_DIR / "database.db"))

TOKEN = os.getenv("DISCORD_TOKEN", "")
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


# ================= Database Helper =================
async def init_db(db: aiosqlite.Connection):
    """สร้างตารางในฐานข้อมูล (ทำงานครั้งเดียวตอนเปิดบอท)"""
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
# TICKET VIEWS
# --------------------------------------------------------------------------------------

class TicketPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) 

    @discord.ui.button(label="📩 เปิด Ticket แจ้งปัญหา", style=discord.ButtonStyle.primary, custom_id="press_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user
        db = interaction.client.db  # ดึง Connection จาก Client

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

        await db.execute("INSERT INTO tickets (ticket_channel_id, user_id) VALUES (?, ?)", (ticket_channel.id, user.id))
        await db.commit()

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
        db = interaction.client.db
        await interaction.response.send_message("กำลังปิดและลบห้องนี้ใน 5 วินาที...")

        await db.execute("DELETE FROM tickets WHERE ticket_channel_id = ?", (interaction.channel.id,))
        await db.commit()

        await asyncio.sleep(5)
        await interaction.channel.delete()


# --------------------------------------------------------------------------------------
# BOT CORE
# --------------------------------------------------------------------------------------

class SocietBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db: aiosqlite.Connection = None # เตรียมตัวแปรเก็บ DB Connection

    async def setup_hook(self) -> None:
        # 1. Initialize Persistent Database Connection
        self.db = await aiosqlite.connect(DB_NAME)
        self.db.row_factory = aiosqlite.Row
        await init_db(self.db)
        
        await self.tree.sync()
        
        # 2. Add Persistent Views
        self.add_view(TicketPersistentView())
        self.add_view(TicketCloseView())
        
        # 3. Start Background Tasks
        if not check_meetings.is_running():
            check_meetings.start()
            
        # 4. Start Web Server
        self.web_app = build_app(self)
        self.web_runner = web.AppRunner(self.web_app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, WEB_HOST, WEB_PORT)
        await site.start()
        print(f"[Web] Societ portal listening on http://{WEB_HOST}:{WEB_PORT}")

    async def close(self):
        if self.db:
            await self.db.close()
        if hasattr(self, "web_runner"):
            await self.web_runner.cleanup()
        await super().close()

bot = SocietBot()

# --------------------------------------------------------------------------------------
# DISCORD EVENTS & TASKS
# --------------------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[Bot] Logged in as {bot.user}")

@bot.event
async def on_member_join(member):
    async with bot.db.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (member.guild.id,)) as cursor:
        row = await cursor.fetchone()

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
    if message.author.bot or not message.guild:
        return
    async with bot.db.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (message.guild.id,)) as cursor:
        row = await cursor.fetchone()

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
    if not member.guild:
        return
    async with bot.db.execute("SELECT voice_master_id, voice_category_id FROM settings WHERE guild_id = ?", (member.guild.id,)) as cursor:
        row = await cursor.fetchone()

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

@tasks.loop(seconds=30)
async def check_meetings():
    now = datetime.datetime.now()
    now_tz = now.astimezone(ZoneInfo("Asia/Bangkok"))
    now_str = now_tz.strftime("%Y-%m-%d %H:%M")

    async with bot.db.execute("SELECT id, topic, channel_id, mention_id FROM schedules WHERE meeting_time <= ? AND is_done = 0", (now_str,)) as cursor:
        meetings = await cursor.fetchall()

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

        await bot.db.execute("UPDATE schedules SET is_done = 1 WHERE id = ?", (db_id,))
    
    if meetings:
        await bot.db.commit()

# --------------------------------------------------------------------------------------
# DISCORD COMMANDS & BOT SETUP
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


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="test_welcome", description="🧪 ทดสอบส่งการ์ดต้อนรับสมาชิกใหม่")
async def test_welcome(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["welcome_channel_id"]:
        channel = interaction.guild.get_channel(row["welcome_channel_id"])
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
            await interaction.followup.send("❌ หาห้องต้อนรับไม่เจอ", ephemeral=True)
    else:
        await interaction.followup.send("❌ ยังไม่ได้ตั้งค่าห้องต้อนรับ", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="test_log", description="🧪 ทดสอบส่ง Log ข้อความถูกลบ")
async def test_log(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    conn.close()

    if row and row["log_channel_id"]:
        log_channel = interaction.guild.get_channel(row["log_channel_id"])
        if log_channel:
            embed = discord.Embed(title="🗑️ [TEST LOG] ข้อความถูกลบ", color=discord.Color.red(), timestamp=interaction.created_at)
            embed.add_field(name="คนพิมพ์", value=interaction.user.mention)
            embed.add_field(name="ช่อง", value=interaction.channel.mention)
            embed.add_field(name="ข้อความที่ลบ", value="นี่คือข้อความสมมุติสำหรับทดสอบระบบ Log", inline=False)
            await log_channel.send(embed=embed)
            await interaction.followup.send("✅ ส่ง Log ทดสอบไปที่ห้องแล้ว!", ephemeral=True)
        else:
            await interaction.followup.send("❌ หาห้อง Log ไม่เจอ", ephemeral=True)
    else:
        await interaction.followup.send("❌ ยังไม่ได้ตั้งค่าห้อง Log", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="setup_systems", description="ตั้งค่าห้องต่าง ๆ ของทั้ง 3 ระบบ")
async def setup_systems(
    interaction: discord.Interaction,
    welcome_channel: Optional[discord.TextChannel] = None,
    log_channel: Optional[discord.TextChannel] = None,
    voice_master_channel: Optional[discord.VoiceChannel] = None,
    voice_category: Optional[discord.CategoryChannel] = None
):
    guild_id = interaction.guild.id
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT guild_id FROM settings WHERE guild_id = ?", (guild_id,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO settings (guild_id) VALUES (?)", (guild_id,))

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
            await interaction.response.send_message("❌ ไม่สามารถนัดหมายเวลาในอดีตได้", ephemeral=True)
            return

        save_time_str = input_time.strftime("%Y-%m-%d %H:%M")

        conn = get_conn()
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

        await interaction.response.send_message(embed=embed)

    except ValueError:
        await interaction.response.send_message("❌ รูปแบบวันเวลาผิด! ตัวอย่างที่ถูก: วันที่ `25/06/2026` และ เวลา `14:30`", ephemeral=True)


@bot.tree.command(name="meeting_list", description="ดูรายการนัดหมายประชุมทั้งหมด")
async def meeting_list(interaction: discord.Interaction):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id, topic, meeting_time FROM schedules WHERE is_done = 0")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📅 ไม่มีนัดหมายที่ค้างอยู่", ephemeral=True)
        return

    embed = discord.Embed(title="📋 รายการนัดหมายทั้งหมด", color=discord.Color.orange())
    for row in rows:
        embed.add_field(
            name=f"🆔 ID: {row['id']} | {row['topic']}", 
            value=f"⏰ เวลา: {row['meeting_time']}", 
            inline=False
        )
    await interaction.response.send_message(embed=embed)


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="meeting_delete", description="ลบนัดหมายที่ทำผิด โดยใช้ ID")
async def meeting_delete(interaction: discord.Interaction, db_id: int):
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT topic FROM schedules WHERE id = ? AND is_done = 0", (db_id,))
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message(f"❌ ไม่พบนัดหมายรหัส ID: {db_id}", ephemeral=True)
        conn.close()
        return

    cursor.execute("DELETE FROM schedules WHERE id = ?", (db_id,))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"🗑️ ลบนัดหมาย ID: {db_id} ({row['topic']}) เรียบร้อยแล้ว")


# --------------------------------------------------------------------------------------
# EMPLOYEE MANAGEMENT COMMANDS
# --------------------------------------------------------------------------------------

@bot.tree.command(
    name="add_employee",
    description="🗂️ Add or update an operative record (Admin only)",
)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def add_employee(
    interaction: discord.Interaction,
    member: discord.Member,
    emp_id: str,
    nickname: str,
    position: str,
    mbti: str = "N/A",
    bio: Optional[str] = None,
    gmail: Optional[str] = None,
    points: int = 0,
    is_admin: bool = False,
) -> None:
    conn = get_conn()

    # ตั้งค่าเพื่อให้ดึงข้อมูลผ่านชื่อ Column เช่น row['emp_id'] ได้ไม่ให้อีก
    conn.row_factory = sqlite3.Row

    conn.execute(
        """INSERT INTO employees (discord_id, emp_id, nickname, position, mbti, bio, contact_email, points, is_admin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET
               emp_id = excluded.emp_id,
               nickname = excluded.nickname,
               position = excluded.position,
               mbti = excluded.mbti,
               bio = COALESCE(excluded.bio, employees.bio),
               contact_email = COALESCE(excluded.contact_email, employees.contact_email),
               points = excluded.points,
               is_admin = excluded.is_admin""",
        (
            str(member.id),
            emp_id,
            nickname,
            position,
            mbti,
            bio,
            gmail,
            points,
            int(is_admin),
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)
    ).fetchone()
    conn.close()

    embed = discord.Embed(
        title="✨ Operative Profile Saved",
        description=f"Successfully updated employee record for {member.mention}",
        color=0x34D399,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="📋 Employee ID", value=f"`{row['emp_id']}`", inline=True
    )
    embed.add_field(name="🏷️ Nickname", value=row["nickname"], inline=True)
    embed.add_field(name="💼 Position", value=row["position"], inline=True)
    embed.add_field(name="🧩 MBTI", value=row["mbti"], inline=True)
    embed.add_field(
        name="✉️ Gmail", value=row["contact_email"] or "N/A", inline=True
    )
    embed.add_field(
        name="💰 Points", value=f"**{row['points']}** pts", inline=True
    )
    embed.add_field(
        name="🛠️ Portal Admin",
        value="✅ Yes" if row["is_admin"] else "❌ No",
        inline=False,
    )

    if row["bio"]:
        embed.add_field(
            name="📝 Biography", value=f"> {row['bio']}", inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="list_employees", description="👥 View all employee records in the system")
async def list_employees(interaction: discord.Interaction):
    conn = get_conn()
    rows = conn.execute("SELECT discord_id, emp_id, nickname, position, mbti FROM employees").fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📭 No employee records found in the system.", ephemeral=True)
        return

    embed = discord.Embed(
        title="👥 Operative Roster", 
        description="List of all registered studio employees",
        color=discord.Color.purple()
    )

    for row in rows:
        embed.add_field(
            name=f"⭐ [{row['emp_id'] or 'N/A'}] {row['nickname']}",
            value=f"**💼 Position:** {row['position']} | **🧩 MBTI:** {row['mbti'] or 'N/A'}\n**📱 Discord Account:** <@{row['discord_id']}>",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="view_employee", description="📋 View detailed profile of a specific operative")
async def view_employee(interaction: discord.Interaction, member: discord.Member):
    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message(f"❌ No records found for {member.mention}", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 Profile: {row['nickname']}",
        description=f"Operative details for {member.mention}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📋 Employee ID", value=f"`{row['emp_id'] or 'N/A'}`", inline=True)
    embed.add_field(name="🏷️ Nickname", value=row["nickname"], inline=True)
    embed.add_field(name="💼 Position", value=row["position"], inline=True)
    embed.add_field(name="🧩 MBTI", value=row["mbti"] or "N/A", inline=True)
    embed.add_field(name="✉️ Gmail", value=row["contact_email"] or "N/A", inline=True)
    embed.add_field(name="💰 Points", value=f"**{row['points']}** pts", inline=True)
    embed.add_field(name="🛠️ Portal Admin", value="✅ Yes" if row["is_admin"] else "❌ No", inline=False)

    if row["bio"]:
        embed.add_field(name="📝 Biography", value=f"> {row['bio']}", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="delete_employee", description="🗑️ Remove an operative record (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def delete_employee(interaction: discord.Interaction, member: discord.Member):
    conn = get_conn()
    row = conn.execute("SELECT nickname FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    
    if not row:
        conn.close()
        await interaction.response.send_message(f"❌ No records found for {member.mention}", ephemeral=True)
        return

    conn.execute("DELETE FROM employees WHERE discord_id = ?", (str(member.id),))
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title="🗑️ Record Deleted",
        description=f"Successfully removed **{row['nickname']}** ({member.mention}) from the database.",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rd_employee", description="🎲 Randomly select an operative from the database")
@app_commands.describe(
    position="กรองเฉพาะตำแหน่งที่ต้องการ (หากไม่ระบุจะสุ่มจากทั้งหมด)",
    channel="ช่องที่ต้องการให้ส่งข้อความไป (หากไม่ระบุจะส่งในช่องปัจจุบัน)"
)
async def rd_employee(
    interaction: discord.Interaction, 
    position: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None
):
    conn = get_conn()
    
    # กรองข้อมูลตามตำแหน่งถ้ามีการระบุ
    if position:
        rows = conn.execute("SELECT * FROM employees WHERE position = ?", (position,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM employees").fetchall()
        
    conn.close()

    # หากไม่พบข้อมูล
    if not rows:
        no_data_msg = f"📭 ไม่พบรายชื่อพนักงานในตำแหน่ง `{position}`" if position else "📭 No operatives found in the database."
        await interaction.response.send_message(no_data_msg, ephemeral=True)
        return

    chosen = random.choice(rows)
    embed = discord.Embed(
        title="🎲 Random Operative Selected!",
        description=f"The void has chosen <@{chosen['discord_id']}>!",
        color=0x38BDF8
    )
    embed.add_field(name="📋 Employee ID", value=f"`{chosen['emp_id'] or 'N/A'}`", inline=True)
    embed.add_field(name="🏷️ Nickname", value=chosen['nickname'], inline=True)
    embed.add_field(name="💼 Position", value=chosen['position'], inline=True)

    # ตรวจสอบว่าต้องส่งไปที่ช่องไหน
    target_channel = channel or interaction.channel

    if target_channel == interaction.channel:
        # หากส่งในช่องปัจจุบัน สามารถตอบกลับ Interaction ได้เลย
        await interaction.response.send_message(embed=embed)
    else:
        # หากส่งไปช่องอื่น ให้ส่ง Embed ไปที่ช่องนั้น แล้วตอบกลับผู้ใช้แบบ Ephemeral (เห็นคนเดียว)
        await target_channel.send(embed=embed)
        await interaction.response.send_message(f"✅ สุ่มสำเร็จ! ส่งผลลัพธ์ไปที่ช่อง {target_channel.mention} เรียบร้อยแล้ว", ephemeral=True)

"""
@bot.tree.command(name="work", description="🔨 ทำงานประจำวันเพื่อรับแต้มสะสม")
async def work(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    now = time.time()
    cooldown_seconds = 3600  # คูลดาวน์ 1 ชั่วโมง

    if user_id in work_cooldowns:
        remaining = int(work_cooldowns[user_id] + cooldown_seconds - now)
        if remaining > 0:
            minutes, seconds = divmod(remaining, 60)
            await interaction.response.send_message(
                f"⏳ คุณเพิ่งทำงานไป! โปรดรออีก **{minutes} นาที {seconds} วินาที** ก่อนทำงานครั้งถัดไป",
                ephemeral=True
            )
            return

    conn = get_conn()
    emp_row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (user_id,)).fetchone()
    if not emp_row:
        conn.close()
        await interaction.response.send_message(
            "❌ คุณยังไม่ได้ลงทะเบียนเป็นพนักงานในระบบ (ติดต่อ Admin เพื่อลงทะเบียน)",
            ephemeral=True
        )
        return

    earned = random.randint(15, 50)
    conn.execute("UPDATE employees SET points = points + ? WHERE discord_id = ?", (earned, user_id))
    conn.commit()
    new_points = conn.execute("SELECT points FROM employees WHERE discord_id = ?", (user_id,)).fetchone()["points"]
    conn.close()

    work_cooldowns[user_id] = now

    work_messages = [
        f"💻 **{emp_row['nickname']}** เขียนโค้ดระบบ backend สำเร็จ!",
        f"🎨 **{emp_row['nickname']}** ออกแบบ UI/UX หน้าใหม่สวยงาม!",
        f"🐛 **{emp_row['nickname']}** แก้ไข Bug ร้ายแรงในเกมสำเร็จ!",
        f"🎮 **{emp_row['nickname']}** ทดสอบระบบเกมอย่างเข้มข้น!",
        f"📄 **{emp_row['nickname']}** เขียน Game Design Document เสร็จสมบูรณ์!"
    ]

    embed = discord.Embed(
        title="🔨 ทำงานสำเร็จ!",
        description=random.choice(work_messages),
        color=0x38BDF8
    )
    embed.add_field(name="💰 แต้มที่ได้รับ", value=f"+**{earned}** pts", inline=True)
    embed.add_field(name="💳 แต้มสะสมรวม", value=f"**{new_points}** pts", inline=True)
    await interaction.response.send_message(embed=embed)
"""

@bot.tree.command(name="work", description="ทำงาน")
async def work(interaction: discord.Interaction, member: discord.Member, task: str):
    embed = discord.Embed(
        title="⚠️ get back to work!",
        description=f"📢 {member.mention} ทำงานด้วย!\n\n**📌",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name=f"{interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="ส่งงานด้วยนะ")
    
    await interaction.response.send_message(content=member.mention, embed=embed)

@bot.tree.command(name="points_give", description="💰 มอบแต้มให้พนักงาน (เฉพาะ Admin ของเว็บ/ระบบ)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def points_give(interaction: discord.Interaction, member: discord.Member, amount: int):
    conn = get_conn()
    admin_row = conn.execute("SELECT is_admin FROM employees WHERE discord_id = ?", (str(interaction.user.id),)).fetchone()
    
    if not admin_row or not admin_row["is_admin"]:
        conn.close()
        await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ Admin ในระบบ ไม่สามารถแจกแต้มได้", ephemeral=True)
        return
        
    if amount <= 0:
        conn.close()
        await interaction.response.send_message("❌ จำนวนแต้มต้องมากกว่า 0", ephemeral=True)
        return

    emp_row = conn.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()
    if not emp_row:
        conn.close()
        await interaction.response.send_message(f"❌ ไม่พบข้อมูลพนักงานสำหรับ {member.mention}", ephemeral=True)
        return

    conn.execute("UPDATE employees SET points = points + ? WHERE discord_id = ?", (amount, str(member.id)))
    conn.commit()
    new_points = conn.execute("SELECT points FROM employees WHERE discord_id = ?", (str(member.id),)).fetchone()["points"]
    conn.close()

    embed = discord.Embed(
        title="💰 มอบแต้มสำเร็จ!",
        description=f"มอบ **{amount}** แต้ม ให้แก่ {member.mention}",
        color=0x34D399
    )
    embed.add_field(name="👤 พนักงาน", value=emp_row["nickname"], inline=True)
    embed.add_field(name="💳 แต้มรวมใหม่", value=f"**{new_points}** pts", inline=True)
    await interaction.response.send_message(embed=embed)

# --------------------------------------------------------------------------------------
# SESSION HANDLING & WEB HELPERS
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

async def fetch_employee(app: web.Application, discord_id: str) -> Optional[aiosqlite.Row]:
    db = app['bot'].db
    async with db.execute("SELECT * FROM employees WHERE discord_id = ?", (str(discord_id),)) as cursor:
        return await cursor.fetchone()

def employee_public(row: aiosqlite.Row) -> dict:
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

async def current_user(request: web.Request) -> Optional[dict]:
    payload = read_session(request.cookies.get(SESSION_COOKIE))
    if not payload:
        return None
    row = await fetch_employee(request.app, payload["discord_id"])
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
        user = await current_user(request)
        if not user or user["role"] == "guest":
            raise web.HTTPUnauthorized(text=json.dumps({"error": "employee_login_required"}), content_type="application/json")
        request["user"] = user
        return await handler(request)
    return wrapper

def require_admin(handler: Handler) -> Handler:
    async def wrapper(request: web.Request) -> web.StreamResponse:
        user = await current_user(request)
        if not user or user["role"] != "admin":
            raise web.HTTPForbidden(text=json.dumps({"error": "admin_only"}), content_type="application/json")
        request["user"] = user
        return await handler(request)
    return wrapper

async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    return data

# --------------------------------------------------------------------------------------
# DISCORD OAUTH2 & WEB API
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
    response.set_cookie("societ_oauth_state", state, max_age=600, httponly=True, samesite="Lax", secure=SESSION_SECURE)
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
    avatar_url = f"https://cdn.discordapp.com/avatars/{profile['id']}/{avatar}.png" if avatar else f"https://cdn.discordapp.com/embed/avatars/{(int(profile['id']) >> 22) % 6}.png"
    
    payload = {
        "discord_id": str(profile["id"]),
        "username": profile.get("global_name") or profile.get("username"),
        "avatar_url": avatar_url,
        "exp": int(time.time()) + SESSION_TTL,
    }
    response = web.HTTPFound("/")
    response.set_cookie(SESSION_COOKIE, sign_session(payload), max_age=SESSION_TTL, path="/", httponly=True, samesite="Lax", secure=SESSION_SECURE)
    if "societ_oauth_state" in request.cookies:
        response.del_cookie("societ_oauth_state", path="/")
    raise response

async def auth_logout(request: web.Request) -> web.StreamResponse:
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE)
    return response

async def api_me(request: web.Request) -> web.StreamResponse:
    user = await current_user(request)
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
def build_app(bot_instance) -> web.Application:
    app = web.Application()
    app['bot'] = bot_instance  # เก็บ Instance ของบอทไว้ใน App เพื่อให้ Web API เรียกใช้ DB ได้
    app.add_routes([
        web.get("/", page_index),
        web.get("/operatives", page_operatives),
        web.get("/archives", page_archives),
        web.get("/store", page_store),
        web.get("/admin", page_admin),

        web.get("/auth/login", auth_login),
        web.get("/auth/callback", auth_callback),
        web.post("/auth/logout", auth_logout),
        web.get("/api/me", api_me),
        
        web.get("/api/employees", api_employees),
        web.get("/api/games", api_games),
        web.get("/api/store/items", api_store_items),
        web.post("/api/store/redeem", api_store_redeem),
        web.get("/api/me/transactions", api_my_transactions),
    ])

    assets_dir = BASE_DIR / "assets"
    if assets_dir.exists():
        app.router.add_static("/assets/", assets_dir)
        
    return app


# ================= Entry Point =================
async def main():
    try:
        if not TOKEN:
            print("[Warning] DISCORD_TOKEN is not configured in environment variables!")
            print("[System] Web Portal will keep running. Press Ctrl+C to terminate.")
            app = build_app(bot)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
            await site.start()
            print(f"[Web] Societ portal listening on http://{WEB_HOST}:{WEB_PORT}")
            while True:
                await asyncio.sleep(3600)
        else:
            await bot.start(TOKEN)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[System] Shutting down gracefully...")
    finally:
        if TOKEN and not bot.is_closed():
            await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
