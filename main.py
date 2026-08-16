import asyncio
from aiohttp import web

from config import DISCORD_TOKEN, HOST, PORT
from bot import bot
from web import app

async def start_web():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    print(f"🌐 Web Server is starting on http://{HOST}:{PORT}")
    await site.start()

async def start_bot():
    print("🤖 Discord Bot is starting...")
    await bot.start(DISCORD_TOKEN)

async def main():
    await asyncio.gather(
        start_bot(),
        start_web()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
