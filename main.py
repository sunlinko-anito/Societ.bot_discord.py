import discord
from discord.ext import commands
import os
from myserver import keep_alive

# ตั้งค่า intents (สิทธิ์ที่บอทต้องการ)
intents = discord.Intents.default()
intents.message_content = True  # อนุญาตให้อ่านเนื้อหาข้อความ
intents.members = True           # อนุญาตให้เข้าถึงข้อมูลสมาชิก

# ตั้งค่า prefix คำสั่ง
bot = commands.Bot(command_prefix='!', intents=intents)

# ─── Event: เมื่อบอทพร้อมใช้งาน ───────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f'✅ บอทออนไลน์แล้ว! เข้าสู่ระบบในชื่อ: {bot.user}')
    print(f'🆔 Bot ID: {bot.user.id}')
    print('─' * 40)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="!help | พร้อมรับคำสั่ง"
        )
    )

# ─── Event: เมื่อมีข้อความใหม่ ────────────────────────────────────────────────
@bot.event
async def on_message(message):
    # ไม่ตอบสนองต่อข้อความของบอทเอง
    if message.author == bot.user:
        return

    # ประมวลผลคำสั่งต่าง ๆ
    await bot.process_commands(message)

# ─── Event: เมื่อสมาชิกใหม่เข้าร่วม ──────────────────────────────────────────
@bot.event
async def on_member_join(member):
    channel = discord.utils.get(member.guild.text_channels, name='general')
    if channel:
        await channel.send(f'👋 ยินดีต้อนรับ {member.mention} เข้าสู่เซิร์ฟเวอร์!')

# ─── คำสั่ง: !hello ────────────────────────────────────────────────────────────
@bot.command(name='hello')
async def hello(ctx):
    """ทักทายบอท"""
    await ctx.send(f'สวัสดี {ctx.author.mention}! 👋')

# ─── คำสั่ง: !ping ─────────────────────────────────────────────────────────────
@bot.command(name='ping')
async def ping(ctx):
    """ตรวจสอบความหน่วงของบอท"""
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! ความหน่วง: **{latency} ms**')

# ─── คำสั่ง: !info ─────────────────────────────────────────────────────────────
@bot.command(name='info')
@commands.guild_only()
async def info(ctx):
    """แสดงข้อมูลเซิร์ฟเวอร์ (ใช้ได้เฉพาะในเซิร์ฟเวอร์)"""
    guild = ctx.guild
    embed = discord.Embed(
        title=f'ℹ️ ข้อมูลเซิร์ฟเวอร์: {guild.name}',
        color=discord.Color.blue()
    )
    embed.add_field(name='👑 เจ้าของ', value=guild.owner.mention, inline=True)
    embed.add_field(name='👥 สมาชิก', value=guild.member_count, inline=True)
    embed.add_field(name='📅 สร้างเมื่อ', value=guild.created_at.strftime('%d/%m/%Y'), inline=True)
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    await ctx.send(embed=embed)

# ─── คำสั่ง: !clear ────────────────────────────────────────────────────────────
@bot.command(name='clear')
@commands.has_permissions(manage_messages=True)
@commands.guild_only()
async def clear(ctx, amount: int = 5):
    """ลบข้อความในห้อง (ต้องการสิทธิ์ Manage Messages) สูงสุด 100 ข้อความ"""
    if amount < 1:
        await ctx.send('⚠️ กรุณาระบุจำนวนมากกว่า 0')
        return
    if amount > 100:
        await ctx.send('⚠️ ลบได้สูงสุด 100 ข้อความต่อครั้ง')
        return
    await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f'🗑️ ลบ {amount} ข้อความแล้ว!')
    await msg.delete(delay=3)

# ─── Error Handler ─────────────────────────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    import traceback
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้!')
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send('❓ ไม่พบคำสั่งนี้ ลองพิมพ์ `!help` เพื่อดูคำสั่งทั้งหมด')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('⚠️ กรุณาระบุข้อมูลให้ครบถ้วน')
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send('❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น')
    elif isinstance(error, commands.BadArgument):
        await ctx.send('⚠️ ข้อมูลที่ระบุไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง')
    else:
        print(f'[ERROR] คำสั่ง: {ctx.command} | ผู้ใช้: {ctx.author}')
        traceback.print_exception(type(error), error, error.__traceback__)
        await ctx.send('❌ เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาลองใหม่อีกครั้ง')

# ─── เริ่มต้น Flask Server และรันบอท ──────────────────────────────────────────
keep_alive()  # เริ่ม Flask server เพื่อให้บอทออนไลน์ตลอด 24 ชม.

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("❌ ไม่พบ DISCORD_TOKEN! กรุณาตั้งค่าใน Secrets")

bot.run(TOKEN)
