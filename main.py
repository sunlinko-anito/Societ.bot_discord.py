import asyncio
from aiohttp import web
import config
from database import init_db
from bot import bot
from web import build_app

async def start_web_server():
    """ฟังก์ชันสำหรับรัน Web Server"""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    print(f"[Web] Server started at http://{config.WEB_HOST}:{config.WEB_PORT}")
    
    # วนลูปเพื่อให้ Web Server ไม่ดับ
    while True:
        await asyncio.sleep(3600)

async def main():
    # 1. สร้างตารางฐานข้อมูลให้เรียบร้อยก่อน
    await init_db()
    
    # 2. สร้าง Task รัน Web Server เป็น Background
    asyncio.create_task(start_web_server())
    
    # 3. รัน Discord Bot (Bot จะดึง Event Loop ไว้ไม่ให้โปรแกรมจบการทำงาน)
    if not config.TOKEN:
        print("[Error] DISCORD_TOKEN is missing!")
        return
        
    await bot.start(config.TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[System] Shutting down gracefully...")
