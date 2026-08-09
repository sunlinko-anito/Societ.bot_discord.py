"""Populate database.db with demo operatives, projects and store items.

Usage:  python seed_database.py [--admin-discord-id 123456789012345678]
"""

import argparse
import importlib.util
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_backend():
    spec = importlib.util.spec_from_file_location("societ_backend", BASE_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMPLOYEES = [
    ("100000000000000001", "Nova", "Creative Director", "Charts the studio's course through the void.",
     "nova@societ.studio", 1200, 1),
    ("100000000000000002", "Orion", "Lead Engineer", "Builds the engines that carry our worlds.",
     "orion@societ.studio", 780, 0),
    ("100000000000000003", "Lyra", "Art Director", "Paints nebulae one pixel at a time.",
     "lyra@societ.studio", 640, 0),
    ("100000000000000004", "Vega", "Sound Designer", "Turns silence into gravity.",
     "vega@societ.studio", 410, 0),
]

GAMES = [
    ("Starfall Requiem", "A slow-burn narrative roguelike set on a dying colony ship.",
     "IN DEVELOPMENT", "PC, Steam Deck", None),
    ("Orbital Drift", "Zero-gravity puzzle platformer about salvaging forgotten satellites.",
     "RELEASED", "PC, Switch", None),
    ("Emerald Signal", "Experimental co-op survival prototype built during a 72h jam.",
     "PROTOTYPE", "PC", None),
]

STORE_ITEMS = [
    ("Extra day off", "One additional paid day off, redeemable any sprint.", 800, -1, None, 1),
    ("Societ hoodie", "Limited nebula-print studio hoodie.", 450, 12, None, 1),
    ("Steam gift card", "$20 Steam wallet code delivered by DM.", 300, 25, None, 1),
    ("Team lunch pick", "You choose where the whole studio eats on Friday.", 150, -1, None, 1),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-discord-id",
                        help="Discord ID to register as a portal admin with 1000 points")
    args = parser.parse_args()

    backend = load_backend()
    backend.init_db()

    conn = sqlite3.connect(backend.DB_NAME)
    cursor = conn.cursor()
    cursor.executemany(
        """INSERT OR IGNORE INTO employees
           (discord_id, nickname, position, bio, contact_email, points, is_admin)
           VALUES (?, ?, ?, ?, ?, ?, ?)""", EMPLOYEES)
    cursor.executemany(
        "INSERT INTO games (title, description, status, platforms, image_url) VALUES (?, ?, ?, ?, ?)",
        [g for g in GAMES
         if not cursor.execute("SELECT 1 FROM games WHERE title = ?", (g[0],)).fetchone()])
    cursor.executemany(
        """INSERT INTO store_items (title, description, price_points, stock, image_url, is_active)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [i for i in STORE_ITEMS
         if not cursor.execute("SELECT 1 FROM store_items WHERE title = ?", (i[0],)).fetchone()])

    if args.admin_discord_id:
        cursor.execute(
            """INSERT INTO employees (discord_id, nickname, position, bio, points, is_admin)
               VALUES (?, 'Director', 'Studio Director', 'Portal administrator.', 1000, 1)
               ON CONFLICT(discord_id) DO UPDATE SET is_admin = 1""",
            (args.admin_discord_id,))

    conn.commit()
    conn.close()
    print(f"Seeded {backend.DB_NAME}")


if __name__ == "__main__":
    main()
