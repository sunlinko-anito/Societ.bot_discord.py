import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
from zoneinfo import ZoneInfo
import random
import typing
from typing import Optional

import config
from database import get_db

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

class SocietBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        if not check_meetings.is_running():
            check_meetings.start()

bot = SocietBot()

@bot.event
async def on_ready():
    print(f"=====================================")
    print(f"[Bot] Logged in as {bot.user}")
    print(f"=====================================")

@tasks.loop(seconds=30)
async def check_meetings():
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    now_str = now.strftime("%Y-%m-%d %H:%M")

    async with await get_db() as db:
        async with db.execute("SELECT id, topic, channel_id, mention_id FROM schedules WHERE meeting_time <= ? AND is_done = 0", (now_str,)) as cursor:
            meetings = await cursor.fetchall()

        for meeting in meetings:
            channel = bot.get_channel(meeting["channel_id"])
            if channel:
                alert_embed = discord.Embed(
                    title="🚨 ได้เวลาประชุมแล้ว! 🚨",
                    description=f"**📌 หัวข้อการประชุม:** {meeting['topic']}",
                    color=discord.Color.red()
                )
                mention_text = f"<@&{meeting['mention_id']}>" if channel.guild.get_role(meeting['mention_id']) else f"<@{meeting['mention_id']}>"
                await channel.send(content=mention_text, embed=alert_embed)

            await db.execute("UPDATE schedules SET is_done = 1 WHERE id = ?", (meeting["id"],))
        await db.commit()


# --------------------------------------------------------------------------------------
# SLASH COMMANDS
# --------------------------------------------------------------------------------------

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="test_welcome", description="🧪 ทดสอบส่งการ์ดต้อนรับสมาชิกใหม่")
async def test_welcome(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with await get_db() as db:
        async with db.execute("SELECT welcome_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
            row = await cursor.fetchone()

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

    async with await get_db() as db:
        async with db.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
            row = await cursor.fetchone()

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

    async with await get_db() as db:
        async with db.execute("SELECT guild_id FROM settings WHERE guild_id = ?", (guild_id,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO settings (guild_id) VALUES (?)", (guild_id,))

        if welcome_channel:
            await db.execute("UPDATE settings SET welcome_channel_id = ? WHERE guild_id = ?", (welcome_channel.id, guild_id))
        if log_channel:
            await db.execute("UPDATE settings SET log_channel_id = ? WHERE guild_id = ?", (log_channel.id, guild_id))
        if voice_master_channel:
            await db.execute("UPDATE settings SET voice_master_id = ? WHERE guild_id = ?", (voice_master_channel.id, guild_id))
        if voice_category:
            await db.execute("UPDATE settings SET voice_category_id = ? WHERE guild_id = ?", (voice_category.id, guild_id))

        await db.commit()

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

        async with await get_db() as db:
            await db.execute(
                "INSERT INTO schedules (topic, meeting_time, channel_id, mention_id) VALUES (?, ?, ?, ?)",
                (topic, save_time_str, channel.id, mention_target.id)
            )
            await db.commit()

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
    async with await get_db() as db:
        async with db.execute("SELECT id, topic, meeting_time FROM schedules WHERE is_done = 0") as cursor:
            rows = await cursor.fetchall()

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
    async with await get_db() as db:
        async with db.execute("SELECT topic FROM schedules WHERE id = ? AND is_done = 0", (db_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(f"❌ ไม่พบนัดหมายรหัส ID: {db_id}", ephemeral=True)
            return

        await db.execute("DELETE FROM schedules WHERE id = ?", (db_id,))
        await db.commit()

    await interaction.response.send_message(f"🗑️ ลบนัดหมาย ID: {db_id} ({row['topic']}) เรียบร้อยแล้ว")


# --------------------------------------------------------------------------------------
# EMPLOYEE MANAGEMENT COMMANDS
# --------------------------------------------------------------------------------------

@bot.tree.command(name="add_employee", description="🗂️ Add or update an operative record (Admin only)")
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
    is_admin: bool = False
) -> None:
    async with await get_db() as db:
        await db.execute(
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
            (str(member.id), emp_id, nickname, position, mbti, bio, gmail, points, int(is_admin)),
        )
        await db.commit()
        
        async with db.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)) as cursor:
            row = await cursor.fetchone()

    embed = discord.Embed(
        title="✨ Operative Profile Saved", 
        description=f"Successfully updated employee record for {member.mention}",
        color=0x34D399
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📋 Employee ID", value=f"`{row['emp_id']}`", inline=True)
    embed.add_field(name="🏷️ Nickname", value=row['nickname'], inline=True)
    embed.add_field(name="💼 Position", value=row['position'], inline=True)
    embed.add_field(name="🧩 MBTI", value=row['mbti'], inline=True)
    embed.add_field(name="✉️ Gmail", value=row['contact_email'] or "N/A", inline=True)
    embed.add_field(name="💰 Points", value=f"**{row['points']}** pts", inline=True)
    embed.add_field(name="🛠️ Portal Admin", value="✅ Yes" if row['is_admin'] else "❌ No", inline=False)
    
    if row['bio']:
        embed.add_field(name="📝 Biography", value=f"> {row['bio']}", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="list_employees", description="👥 View all employee records in the system")
async def list_employees(interaction: discord.Interaction):
    async with await get_db() as db:
        async with db.execute("SELECT discord_id, emp_id, nickname, position, mbti FROM employees") as cursor:
            rows = await cursor.fetchall()

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
    async with await get_db() as db:
        async with db.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)) as cursor:
            row = await cursor.fetchone()

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
    async with await get_db() as db:
        async with db.execute("SELECT nickname FROM employees WHERE discord_id = ?", (str(member.id),)) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            await interaction.response.send_message(f"❌ No records found for {member.mention}", ephemeral=True)
            return

        await db.execute("DELETE FROM employees WHERE discord_id = ?", (str(member.id),))
        await db.commit()

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
    position: typing.Optional[str] = None,
    channel: typing.Optional[discord.TextChannel] = None
):
    async with await get_db() as db:
        if position:
            async with db.execute("SELECT * FROM employees WHERE position = ?", (position,)) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute("SELECT * FROM employees") as cursor:
                rows = await cursor.fetchall()

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

    target_channel = channel or interaction.channel

    if target_channel == interaction.channel:
        await interaction.response.send_message(embed=embed)
    else:
        await target_channel.send(embed=embed)
        await interaction.response.send_message(f"✅ สุ่มสำเร็จ! ส่งผลลัพธ์ไปที่ช่อง {target_channel.mention} เรียบร้อยแล้ว", ephemeral=True)


@bot.tree.command(name="work", description="ทำงาน")
async def work(interaction: discord.Interaction, member: discord.Member, task: str):
    embed = discord.Embed(
        title="⚠️ get back to work!",
        description=f"📢 {member.mention} ทำงานด้วย!\n\n**📌 {task}**",
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
    async with await get_db() as db:
        async with db.execute("SELECT is_admin FROM employees WHERE discord_id = ?", (str(interaction.user.id),)) as cursor:
            admin_row = await cursor.fetchone()
        
        if not admin_row or not admin_row["is_admin"]:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ Admin ในระบบ ไม่สามารถแจกแต้มได้", ephemeral=True)
            return
            
        if amount <= 0:
            await interaction.response.send_message("❌ จำนวนแต้มต้องมากกว่า 0", ephemeral=True)
            return

        async with db.execute("SELECT * FROM employees WHERE discord_id = ?", (str(member.id),)) as cursor:
            emp_row = await cursor.fetchone()

        if not emp_row:
            await interaction.response.send_message(f"❌ ไม่พบข้อมูลพนักงานสำหรับ {member.mention}", ephemeral=True)
            return

        await db.execute("UPDATE employees SET points = points + ? WHERE discord_id = ?", (amount, str(member.id)))
        await db.commit()

        async with db.execute("SELECT points FROM employees WHERE discord_id = ?", (str(member.id),)) as cursor:
            res = await cursor.fetchone()
            new_points = res["points"]

    embed = discord.Embed(
        title="💰 มอบแต้มสำเร็จ!",
        description=f"มอบ **{amount}** แต้ม ให้แก่ {member.mention}",
        color=0x34D399
    )
    embed.add_field(name="👤 พนักงาน", value=emp_row["nickname"], inline=True)
    embed.add_field(name="💳 แต้มรวมใหม่", value=f"**{new_points}** pts", inline=True)
    await interaction.response.send_message(embed=embed)
