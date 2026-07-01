from ctypes import Union
import os
import discord
import random as rd
import numpy as np
import asyncio 
import datetime
from zoneinfo import ZoneInfo
import sqlite3
from discord.ext import commands, tasks
from discord import app_commands
from myserver import keep_alive

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Environment variable DISCORD_TOKEN is not set.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_NAME = "company_bot.db"


def init_db():
    """สร้างตารางสำหรับเก็บข้อมูลพนักงานและการนัดประชุม"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # บังคับสร้างตาราง Schedules
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

    # บังคับสร้างตาราง Employees
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS employees (
           discord_id INTEGER PRIMARY KEY,
           emp_id TEXT NOT NULL,
           nickname TEXT,
           position TEXT,
           mbti TEXT
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
    print("💾 [Database] บังคับสร้างและเชื่อมต่อตารางข้อมูลทั้งหมดเรียบร้อยแล้ว!")


@bot.event
async def on_ready():
    init_db()
    print("=================================")
    print(f"                               Logged in as {bot.user.name}")
    print("=================================")

    if not check_meetings.is_running():
        check_meetings.start()

    # [แก้ไขจุดที่ 2] ลงทะเบียน Views ทั้งสองแบบเพื่อให้ปุ่มยังคงทำงานได้หลังจากเปิดบอทใหม่
    bot.add_view(TicketPersistentView())
    bot.add_view(TicketCloseView())

    try:
        GUILD_ID = discord.Object(id=1497527309413122089) 
        
        synced = await bot.tree.sync(guild=GUILD_ID)
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)


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


# ------------------------------------------------------------------------------------- SLASH COMMAND ------------------------------------------------------------------------------------------


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
        await interaction.followup.send("❌ คุณยังไม่ได้ตั้งค่าห้อง Log กรุณาใช้ `/setup_systems` ตั้งอัปเดตก่อน", ephemeral=True)


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
    await interaction.response.send_message("⚙️ อัปเดตการตั้งค่าระบบสำเร็จแล้ว!", ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="send_ticket_button", description="ส่งปุ่มกดสร้าง Ticket ลงในช่องปัจจุบัน")
async def send_ticket_button(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📞 ศูนย์บริการช่วยเหลือสมาชิก (Support Ticket)",
        description="หากพบปัญหาการใช้งาน, ต้องการแจ้งรีพอร์ตผู้เล่น หรือติดต่อสอบถามทีมงานแอดมิน\nกรุณากดปุ่มด้านล่างนี้เพื่อเปิดห้องแชทคุยตัวต่อตัวแบบส่วนตัว",
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
    mention_target="บทบาท (Role) หรือผู้ใช้ที่จะแท็กตามตัวเพื่อเรียกประชุม"
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


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="add_employee", description="เพิ่มข้อมูลหรือแก้ไขข้อมูลพนักงาน")
@app_commands.describe(
    member="เลือกบัญชี Discord ของพนักงาน",
    emp_id="รหัสพนักงาน",
    nickname="ชื่อเล่น",
    position="ตำแหน่งหน้าที่",
    mbti="MBTI (เช่น INTJ, ENFP)"
)
async def add_employee(
    interaction: discord.Interaction,
    member: discord.Member,
    emp_id: str,
    nickname: str,
    position: str,
    mbti: str
):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO employees (discord_id, emp_id, nickname, position, mbti)
        VALUES (?, ?, ?, ?, ?)
    """, (member.id, emp_id, nickname, position, mbti.upper()))

    conn.commit()
    conn.close()

    embed = discord.Embed(title="✨ บันทึกข้อมูลพนักงานสำเร็จ", color=discord.Color.green())
    embed.add_field(name="💳 รหัสพนักงาน", value=emp_id, inline=True)
    embed.add_field(name="👤 ชื่อเล่น", value=nickname, inline=True)
    embed.add_field(name="💼 ตำแหน่งหน้าที่", value=position, inline=True)
    embed.add_field(name="🧠 MBTI", value=mbti.upper(), inline=True)
    embed.add_field(name="🌐 บัญชี Discord", value=member.mention, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="list_employees", description="ดูรายชื่อและข้อมูลพนักงานทั้งหมดในระบบ")
async def list_employees(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, emp_id, nickname, position, mbti FROM employees")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📭 ยังไม่มีข้อมูลพนักงานในระบบ", ephemeral=True)
        return

    embed = discord.Embed(title="👥 รายชื่อพนักงานทั้งหมดในระบบ", color=discord.Color.purple())

    for row in rows:
        discord_id, emp_id, nickname, position, mbti = row
        embed.add_field(
            name=f"⭐ [{emp_id}] คุณ {nickname}",
            value=f"**ตำแหน่ง:** {position} | **MBTI:** {mbti}\n**บัญชี Discord:** <@{discord_id}>",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="view_employee", description="ดูข้อมูลพนักงานเฉพาะบุคคล")
@app_commands.describe(member="เลือกบัญชี Discord ของพนักงานที่ต้องการดูข้อมูล")
async def view_employee(interaction: discord.Interaction, member: discord.Member):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT emp_id, nickname, position, mbti FROM employees WHERE discord_id = ?", (member.id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        emp_id, nickname, position, mbti = row
        embed = discord.Embed(title=f"🔎 ข้อมูลพนักงาน: {nickname}", color=discord.Color.blue())
        embed.add_field(name="💳 รหัสพนักงาน", value=emp_id, inline=True)
        embed.add_field(name="👤 ชื่อเล่น", value=nickname, inline=True)
        embed.add_field(name="💼 ตำแหน่งหน้าที่", value=position, inline=True)
        embed.add_field(name="🧠 MBTI", value=mbti, inline=True)
        embed.add_field(name="🌐 บัญชี Discord", value=member.mention, inline=False)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(f"❌ ไม่พบข้อมูลพนักงานของ {member.mention} ในระบบ", ephemeral=True)


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="delete_employee", description="ลบข้อมูลพนักงานออกจากระบบ")
@app_commands.describe(member="เลือกบัญชี Discord ของพนักงานที่ต้องการลบ")
async def delete_employee(interaction: discord.Interaction, member: discord.Member):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT nickname FROM employees WHERE discord_id = ?", (member.id,))
    row = cursor.fetchone()

    if row:
        nickname = row[0]
        cursor.execute("DELETE FROM employees WHERE discord_id = ?", (member.id,))
        conn.commit()
        conn.close()

        await interaction.response.send_message(f"🗑️ ลบข้อมูลของ คุณ **{nickname}** ({member.mention}) ออกจากระบบเรียบร้อยแล้ว")
    else:
        conn.close()
        await interaction.response.send_message(f"❌ ไม่พบข้อมูลของ {member.mention} ในระบบ", ephemeral=True)


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


@bot.tree.command(name="test", description="test command!")
async def test(interaction: discord.Interaction):
    try:
        await interaction.response.send_message(f"test by {interaction.user.mention}!", ephemeral=False)
    except Exception as e:
        await interaction.response.send_message("An error occurred.", ephemeral=True)
        print(f"Slash command error: {e}")


@bot.tree.command(name="work", description="work!")
@app_commands.describe(channel="เลือกห้องที่ต้องการส่ง", member="เลือกคนที่ต้องการแท็ก")
async def work(interaction: discord.Interaction, channel: discord.TextChannel, member: discord.Member):
    await channel.send(f"get back to work and submit your work too. {member.mention}!")
    await interaction.response.send_message(f"mention {member.display_name} to {channel.mention} completed successfully", ephemeral=True)


async def main():
    keep_alive()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except discord.LoginFailure:
        print("Invalid bot token. Check your DISCORD_TOKEN environment variable.")
    except Exception as e:
        print(f"Unexpected error: {e}")