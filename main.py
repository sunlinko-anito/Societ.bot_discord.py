import asyncio
import hypercorn.asyncio
from hypercorn.config import Config

from config import DISCORD_TOKEN, HOST, PORT
from bot import bot
from web import app

async def start_web():
    config = Config()
    config.bind = [f"{HOST}:{PORT}"]
    print(f"🌐 Web Server is starting on http://{HOST}:{PORT}")
    await hypercorn.asyncio.serve(app, config)

async def start_bot():
    print("🤖 Discord Bot is starting...")
    await bot.start(DISCORD_TOKEN)

async def main():
    await asyncio.gather(
        start_bot(),
        start_web()
    )

if __name__ == "__main__":
    asyncio.run(main())
