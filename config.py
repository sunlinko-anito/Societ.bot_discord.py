import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = os.getenv("SOCIET_DB", str(BASE_DIR / "database.db"))

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", 0)) if os.getenv("GUILD_ID") else 0
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
WEB_PORT = int(os.getenv("SERVER_PORT", os.getenv("PORT", "13660")))
DISCORD_API = "https://discord.com/api/v10"

# --- ตัวแปรสำรองเพื่อให้ main.py และไฟล์อื่นๆ เรียกใช้ได้แบบไม่มีปัญหา ---
DISCORD_TOKEN = TOKEN
HOST = WEB_HOST
PORT = WEB_PORT
