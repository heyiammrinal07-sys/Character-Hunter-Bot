# main.py
import os
import asyncio
import random
import time
import json
import logging
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from motor.motor_asyncio import AsyncIOMotorClient

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- ENV VARS ----------
TOKEN = os.getenv("TOKEN")  # set in Heroku Config Vars
MONGODB_URI = os.getenv("MONGODB_URI")  # set in Heroku Config Vars
DB_NAME = os.getenv("DB_NAME", "waifu_catcher_db")

if not TOKEN:
    logger.error("TOKEN not set. Exiting.")
    raise SystemExit("TOKEN environment variable required.")
if not MONGODB_URI:
    logger.error("MONGODB_URI not set. Exiting.")
    raise SystemExit("MONGODB_URI environment variable required.")

# ---------- BOT / DP ----------
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- DB CLIENT ----------
mongo = AsyncIOMotorClient(MONGODB_URI)
db = mongo[DB_NAME]

# Collections:
# db.waifus  - documents of waifus {id,name,img,tags}
# db.users   - per user {user_id, waifus: [{waifu_id,count,rarity,...}], stats...}
# db.pending - pending catches for user {user_id, waifu_id, rarity, ts}
# db.meta    - any meta info

# ---------- SETTINGS ----------
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN", "15"))
RARITY_WEIGHTS = [
    ("Legendary", 2),   # 2%
    ("Epic", 8),        # 8%
    ("Rare", 20),       # 20%
    ("Common", 70)      # 70%
]
# convert weights to normalized list for random choice
def build_rarity_choice():
    items = []
    for r, w in RARITY_WEIGHTS:
        items.extend([r] * w)
    return items

RARITY_CHOICES = build_rarity_choice()

# ---------- HELPERS ----------
async def ensure_waifus_loaded():
    existing = await db.waifus.count_documents({})
    if existing > 0:
        logger.info(f"Waifus already present: {existing}")
        return
    # load local waifus.json (should be included in repo)
    here = os.path.dirname(__file__)
    path = os.path.join(here, "waifus.json")
    if not os.path.exists(path):
        logger.warning("waifus.json not found; start with empty DB.")
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        logger.warning("waifus.json should contain a JSON array.")
        return
    docs = []
    for item in data:
        doc = {
            "waifu_id": item.get("id") or item.get("name"),
            "name": item.get("name"),
            "img": item.get("img"),
            "tags": item.get("tags", [])
        }
        docs.append(doc)
    if docs:
        await db.waifus.insert_many(docs)
        logger.info(f"Inserted {len(docs)} waifus to DB.")

def choose_random_waifu():
    # choose random waifu document id from DB
    # We'll pick a random document by sampling count + skip (simple approach)
    # For large collections consider preloading IDs or use aggregation $sample
    return None  # placeholder; we use async version below

async def choose_random_waifu_async():
    count = await db.waifus.count_documents({})
    if count == 0:
        return None
    # use aggregation sample for randomness
    cur = db.waifus.aggregate([{"$sample": {"size": 1}}])
    docs = await cur.to_list(1)
    return docs[0] if docs else None

def pick_rarity():
    # choose from weighted list built above
    return random.choice(RARITY_CHOICES)

# ---------- COMMANDS ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    text = (
        "🔥 Welcome to Waifu Catcher!\n\n"
        "Commands:\n"
        "/catch - roll for a waifu (then /claim)\n"
        "/claim - claim your last rolled waifu\n"
        "/inventory - see your collection\n"
        "/profile - your stats\n"
        "/leaderboard - top collectors\n"
    )
    await msg.answer(text)

@dp.message(Command("catch"))
async def cmd_catch(msg: types.Message):
    user_id = msg.from_user.id

    # cooldown check
    pending = await db.pending.find_one({"user_id": user_id})
    last_ts = 0
    if pending and "ts" in pending:
        last_ts = pending["ts"]
    # But we'll store cooldown per-user in users collection as well
    userdoc = await db.users.find_one({"user_id": user_id})
    last_catch = userdoc.get("last_catch", 0) if userdoc else 0
    now = int(time.time())
    if now - last_catch < COOLDOWN_SECONDS:
        remain = COOLDOWN_SECONDS - (now - last_catch)
        await msg.answer(f"⏳ Wait {remain}s before catching again.")
        return

    # pick waifu
    w = await choose_random_waifu_async()
    if not w:
        await msg.answer("No waifus loaded yet. Contact the admin.")
        return

    rarity = pick_rarity()

    # save pending
    await db.pending.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "waifu_id": w["waifu_id"], "name": w["name"], "img": w.get("img"), "rarity": rarity, "ts": now}},
        upsert=True
    )
    # update user's last_catch timestamp
    await db.users.update_one({"user_id": user_id}, {"$set": {"last_catch": now}}, upsert=True)

    caption = (
        f"🎴 A Waifu appeared!\n\n"
        f"❤️ Name: {w['name']}\n"
        f"⭐ Rarity: {rarity}\n\n"
        "Use /claim to add her to your collection."
    )
    if w.get("img"):
        try:
            await msg.answer_photo(w.get("img"), caption)
        except Exception:
            await msg.answer(caption)
    else:
        await msg.answer(caption)

@dp.message(Command("claim"))
async def cmd_claim(msg: types.Message):
    user_id = msg.from_user.id
    pend = await db.pending.find_one({"user_id": user_id})
    if not pend:
        await msg.answer("❌ You have no waifu to claim. Use /catch first.")
        return

    # add to user's collection (increment count if already owned)
    waifu_entry = {
        "waifu_id": pend["waifu_id"],
        "name": pend["name"],
        "img": pend.get("img"),
        "rarity": pend["rarity"],
        "claimed_ts": int(time.time()),
    }
    # upsert: if user doc exists update list
    await db.users.update_one(
        {"user_id": user_id},
        {"$inc": {}, "$setOnInsert": {"user_id": user_id}},
        upsert=True
    )

    # increment if exists in subcollection, else push new object with count
    # We'll store collection as dict: waifus_map: {waifu_id: {name,img,rarity,count}}
    user_doc = await db.users.find_one({"user_id": user_id})
    waifus_map = user_doc.get("waifus_map", {}) if user_doc else {}

    wid = waifu_entry["waifu_id"]
    if wid in waifus_map:
        waifus_map[wid]["count"] = waifus_map[wid].get("count", 1) + 1
    else:
        waifus_map[wid] = {
            "name": waifu_entry["name"],
            "img": waifu_entry.get("img"),
            "rarity": waifu_entry["rarity"],
            "count": 1
        }

    await db.users.update_one({"user_id": user_id}, {"$set": {"waifus_map": waifus_map}})

    # remove pending
    await db.pending.delete_one({"user_id": user_id})

    await msg.answer(f"✅ You claimed {waifus_map[wid]['name']} ({waifus_map[wid]['rarity']}). You now have {waifus_map[wid]['count']} of them.")

@dp.message(Command("inventory"))
async def cmd_inventory(msg: types.Message):
    user_id = msg.from_user.id
    user_doc = await db.users.find_one({"user_id": user_id})
    waifus_map = user_doc.get("waifus_map", {}) if user_doc else {}
    if not waifus_map:
        await msg.answer("📦 Your collection is empty.")
        return
    # Build text (first 20 items)
    lines = []
    for i, (wid, info) in enumerate(sorted(waifus_map.items(), key=lambda x: (-x[1].get("count",0), x[1].get("rarity",""))), 1):
        lines.append(f"{i}. {info['name']} — {info['rarity']} x{info['count']}")
        if i >= 20: break
    txt = "📦 Your collection:\n\n" + "\n".join(lines)
    await msg.answer(txt)

@dp.message(Command("profile"))
async def cmd_profile(msg: types.Message):
    user_id = msg.from_user.id
    user_doc = await db.users.find_one({"user_id": user_id})
    waifus_map = user_doc.get("waifus_map", {}) if user_doc else {}
    total = sum(info.get("count", 0) for info in waifus_map.values())
    rarity_counts = {}
    for info in waifus_map.values():
        rarity_counts[info.get("rarity","Unknown")] = rarity_counts.get(info.get("rarity","Unknown"), 0) + info.get("count",0)
    rtext = "\n".join(f"{k}: {v}" for k,v in rarity_counts.items()) or "None"
    await msg.answer(f"👤 Profile\nTotal waifus: {total}\n\nRarity counts:\n{rtext}")

@dp.message(Command("leaderboard"))
async def cmd_leaderboard(msg: types.Message):
    # aggregate top users by total waifus
    cursor = db.users.find({})
    docs = await cursor.to_list(length=100)
    scores = []
    for d in docs:
        wm = d.get("waifus_map", {})
        total = sum(v.get("count",0) for v in wm.values())
        if total > 0:
            scores.append((d["user_id"], total))
    top = sorted(scores, key=lambda x: -x[1])[:10]
    if not top:
        await msg.answer("No data yet.")
        return
    lines = []
    for rank, (uid, score) in enumerate(top, 1):
        try:
            member = await bot.get_chat(uid)
            name = member.full_name
        except Exception:
            name = str(uid)
        lines.append(f"{rank}. {name} — {score}")
    await msg.answer("🏆 Leaderboard\n\n" + "\n".join(lines))

# ---------- STARTUP ----------

async def on_startup():
    logger.info("Starting up: ensuring waifus loaded...")
    await ensure_waifus_loaded()
    logger.info("Bot started.")

async def main():
    await on_startup()
    try:
        logger.info("Polling started.")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
