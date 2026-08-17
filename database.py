import aiosqlite
import config
from contextlib import asynccontextmanager

@asynccontextmanager
async def _db_connection():
    """จัดการการเชื่อมต่อฐานข้อมูลให้รองรับ async with โดยไม่ให้ thread ชนกัน"""
    async with aiosqlite.connect(config.DB_NAME) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys = ON;")
        yield conn

async def get_db():
    """ฟังก์ชันคืนค่า Context Manager เพื่อให้ bot.py ใช้งานคำสั่งเดิมได้ทันที"""
    return _db_connection()

async def init_db():
    """สร้างตารางในฐานข้อมูล (ทำงานครั้งเดียวตอนเปิดระบบ)"""
    async with await get_db() as db:
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
        print(f"[Database] Tables ready in {config.DB_NAME} (WAL Mode Enabled)")
        
