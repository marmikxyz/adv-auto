import asyncio
import html
import logging
import os
import re
import secrets
import time
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterDocument, InputMessagesFilterVideo

import db

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# ------------------------------------------------------------------ config
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "").split(",") if c.strip()]
OWNERS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
CACHE_DAYS = int(os.environ.get("CACHE_DAYS", "30"))
PER_CHANNEL = int(os.environ.get("PER_CHANNEL_LIMIT", "200"))
GROUP_FILES_TO_DM = os.environ.get("GROUP_FILES_TO_DM", "true").lower() == "true"
CLEAN_MIN = float(os.environ.get("CLEAN_MIN", "1"))
FILE_DELETE_MIN = float(os.environ.get("FILE_DELETE_MIN", "10"))
WELCOME = (os.environ.get("WELCOME_TEXT") or
           "👋 Hello <b>{name}</b>!\n\nSend me the name of a movie or series and I will find it for you."
           ).replace("\\n", "\n")
WELCOME_PHOTO = os.environ.get("WELCOME_PHOTO", "")
SUPPORT_URL = os.environ.get("SUPPORT_URL", "")

PAGE = 10
MAX_RESULTS = 300
COOLDOWN = 3.0
ICON = {"video": "🎬", "document": "📁", "audio": "🎵"}

user: TelegramClient | None = None   # your Telethon account (created in post_init)
CHANNEL_ENTS: list = []              # resolved channels


# ------------------------------------------------------------------ text helpers
def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^\w\s]|_", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/)\S+", re.I)
USER_RE = re.compile(r"@[A-Za-z0-9_]{3,}")


def clean_caption(s: str) -> str:
    """remove links and @usernames"""
    s = URL_RE.sub("", s or "")
    s = USER_RE.sub("", s)
    s = re.sub(r"[\[\(\{]\s*[\]\)\}]", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"^[\s\-–—|•:]+|[\s\-–—|•:]+$", "", s, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# words that end the "name" part:  "Ace 2024 1080p Hindi"  ->  name = "ace"
STOP = re.compile(
    r"^(?:(?:19|20)\d{2}|s\d{1,2}(?:e\d{1,3})?|e\d{1,3}|ep\d{1,3}|season|episode|complete|\d{3,4}p|4k|uhd|"
    r"hdrip|webrip|webdl|bluray|brrip|bdrip|dvdrip|hdtv|hdcam|camrip|hdts|x264|x265|h264|h265|hevc|10bit|"
    r"dual|multi|hindi|hin|english|eng|tamil|telugu|malayalam|kannada|bengali|punjabi|korean|japanese|"
    r"esub|esubs|mkv|mp4|avi)$"
)


def extract_title(text: str) -> str:
    out = []
    for i, t in enumerate(text.split()):
        if i > 0 and STOP.match(t):
            break
        out.append(t)
    return " ".join(out)


def detect(n: str) -> dict:
    sm = re.search(r"\b(?:season\s*|s)0*(\d{1,2})(?!\d)", n)
    qm = re.search(r"\b(2160p|4k|1080p|720p|480p|360p)\b", n)
    em = re.search(r"\bs\d{1,2}\s*e0*(\d{1,3})\b", n) or re.search(r"\b(?:episode|ep|e)\s*0*(\d{1,3})\b", n)
    dual = bool(re.search(r"\b(?:dual audio|multi audio)\b", n))
    return {
        "season": int(sm.group(1)) if sm else 0,
        "quality": (2160 if qm.group(1) == "4k" else int(qm.group(1)[:-1])) if qm else 0,
        "episode": int(em.group(1)) if em else 0,
        "hi": dual or bool(re.search(r"\b(?:hindi|hin|hind)\b", n)),
        "en": dual or bool(re.search(r"\b(?:english|eng)\b", n)),
    }


NOISE = {"movie", "movies", "film", "full", "download", "free", "watch", "online", "link", "file", "please", "pls"}


def parse_query(q: str) -> dict:
    """'money heist s02 720p hindi' -> words + season 2 + quality 720 + hindi"""
    toks = norm(q).split()
    season = quality = 0
    lang = year = ""
    rest, i = [], 0
    while i < len(toks):
        t = toks[i]
        m = re.fullmatch(r"s0*(\d{1,2})", t)
        if m:
            season = int(m.group(1))
        elif t == "season" and i + 1 < len(toks) and toks[i + 1].isdigit():
            season = int(toks[i + 1])
            i += 1
        elif re.fullmatch(r"(?:2160|1080|720|480|360)p", t):
            quality = int(t[:-1])
        elif t == "4k":
            quality = 2160
        elif t in ("hindi", "hin"):
            lang = "hindi"
        elif t in ("english", "eng"):
            lang = "english"
        elif re.fullmatch(r"(?:19|20)\d{2}", t) and not year:
            year = t
        elif t in NOISE:
            pass
        else:
            rest.append(t)
        i += 1
    if not rest:  # only filters / a year -> use it as the name
        plain = [t for t in toks if t not in NOISE] or toks
        return {"words": plain, "season": 0, "quality": 0, "lang": "", "year": ""}
    return {"words": rest, "season": season, "quality": quality, "lang": lang, "year": year}


def rank(rows, pq: dict) -> list[dict]:
    """best group only: 1) name == keyword / starts with it  2) keyword inside the name
    3) keyword anywhere as a whole word  4) anywhere.  Duplicates (same name + size) are dropped."""
    words = pq["words"]
    q = " ".join(words)
    groups: list[list] = [[], [], [], []]
    for r in rows:
        n = r["norm"]
        t = r["title"] or n
        d = detect(n)
        if pq["season"] and d["season"] != pq["season"]:
            continue
        if pq["quality"] and d["quality"] != pq["quality"]:
            continue
        if pq["lang"] == "hindi" and not d["hi"]:
            continue
        if pq["lang"] == "english" and not d["en"]:
            continue
        if pq["year"] and pq["year"] not in n.split():
            continue
        tp = f" {t} "
        if t == q:
            g, tier = 0, 0
        elif t.startswith(q + " "):
            g, tier = 0, 1
        elif f" {q} " in tp or all(f" {w} " in tp for w in words):
            g, tier = 1, 2
        elif all(w in n.split() for w in words):
            g, tier = 2, 3
        else:
            g, tier = 3, 4
        groups[g].append(((tier, t, d["season"], d["episode"], d["quality"], -r["id"]), r))
    chosen = next((g for g in groups if g), [])
    chosen.sort(key=lambda x: x[0])
    seen, out = set(), []
    for _, r in chosen:
        key = (r["norm"], r["size"] // 1048576)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": r["id"], "caption": r["caption"], "size": r["size"], "kind": r["kind"]})
        if len(out) >= MAX_RESULTS:
            break
    return out


def fmt_size(b: int) -> str:
    if not b:
        return ""
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f}GB"
    if b >= 1 << 20:
        return f"{b // (1 << 20)}MB"
    return f"{b // 1024}KB"


# ------------------------------------------------------------------ indexing (Telethon -> database)
def media_kind(m) -> str | None:
    if m.video:
        return "video"
    if m.audio:
        return "audio"
    if m.document:
        return "document"
    return None


async def index_message(m) -> bool:
    """save one channel message in the database; True if it was new"""
    kind = media_kind(m)
    if not kind or not m.file:
        return False
    name = clean_caption(m.file.name or "")
    caption = clean_caption(m.message or "") or name or "Untitled"
    n = norm(f"{caption} {name}")
    title = extract_title(norm(caption)) or extract_title(n)
    return await db.upsert_file(m.chat_id, m.id, kind, name, caption, title, n, m.file.size or 0)


async def on_new_channel_post(event) -> None:
    try:
        await index_message(event.message)
    except Exception:
        log.exception("live index failed")


async def resolve_channels() -> list:
    await user.get_dialogs()  # fills Telethon's cache so numeric ids can be resolved
    ents = []
    for raw in CHANNELS:
        try:
            key = int(raw) if raw.lstrip("-").isdigit() else raw
            ent = await user.get_entity(key)
            ents.append(ent)
            log.info("channel ok: %s", getattr(ent, "title", raw))
        except Exception as e:
            log.error("cannot open channel %s: %s (is your account a member?)", raw, e)
    return ents


_inflight: dict[str, asyncio.Task] = {}


async def telethon_search(kw: str) -> None:
    """search all database channels with YOUR account and store every file found"""
    sem = asyncio.Semaphore(3)
    failed = False

    async def one(ent) -> int:
        nonlocal failed
        found = 0
        async with sem:
            try:
                async for m in user.iter_messages(ent, search=kw, limit=PER_CHANNEL):
                    if await index_message(m):
                        found += 1
            except FloodWaitError as e:
                failed = True
                log.warning("FloodWait %ss on %s", e.seconds, getattr(ent, "title", ent))
            except Exception:
                failed = True
                log.exception("search failed in %s", getattr(ent, "title", ent))
        return found

    res = await asyncio.gather(*(one(e) for e in CHANNEL_ENTS))
    if not failed:  # only cache the keyword when every channel answered
        await db.mark_keyword(kw, sum(res))


async def ensure_searched(kw: str) -> None:
    if await db.keyword_fresh(kw, CACHE_DAYS):
        return  # searched before -> Telethon is NOT used
    task = _inflight.get(kw)
    if task is None:  # the same keyword at the same time is searched only once
        task = asyncio.create_task(telethon_search(kw))
        _inflight[kw] = task
        task.add_done_callback(lambda _t: _inflight.pop(kw, None))
    await asyncio.shield(task)


async def get_results(pq: dict) -> list[dict]:
    kw = " ".join(pq["words"])  # "ace 720p" and "ace" share one Telethon search
    try:
        await ensure_searched(kw)
    except Exception:
        log.exception("telethon search error, using saved files only")
    rows = await db.find_candidates(pq["words"])
    return rank(rows, pq)


# ------------------------------------------------------------------ sessions (pagination state, in memory)
SESS: dict[str, dict] = {}
COOL: dict[int, float] = {}
_tasks: set = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def new_session(uid: int, name: str, q: str, items: list, ctype: str) -> str:
    sid = secrets.token_urlsafe(5)
    SESS[sid] = {"uid": uid, "name": name, "q": q, "items": items, "ctype": ctype, "at": time.time()}
    while len(SESS) > 500:
        SESS.pop(next(iter(SESS)))
    return sid


def get_session(sid: str) -> dict | None:
    s = SESS.get(sid)
    if s and time.time() - s["at"] > 1800:
        SESS.pop(sid, None)
        return None
    return s


def schedule_delete(bot, chat_id: int, ids: list, minutes: float) -> None:
    if minutes <= 0:
        return

    async def run():
        await asyncio.sleep(minutes * 60)
        for i in ids:
            try:
                await bot.delete_message(chat_id, i)
            except TelegramError:
                pass

    spawn(run())


def render(sid: str, page: int):
    s = SESS[sid]
    items = s["items"]
    pages = max(1, -(-len(items) // PAGE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]
    in_group = s["ctype"] != "private"
    line = "━━━━━━━━━━━━━━━"
    text = f"🔎 <b>{html.escape(s['q'])}</b>\n"
    if in_group:
        text += f'👤 <a href="tg://user?id={s["uid"]}">{html.escape(s["name"])}</a>\n'
    text += f"{line}\n📂 Results: <b>{len(items)}</b>   📄 Page: <b>{page + 1}</b>/<b>{pages}</b>\n{line}\n"
    text += "👇 <i>Tap a file — I will send it to your DM</i>" if in_group and GROUP_FILES_TO_DM \
        else "👇 <i>Tap a file to get it</i>"
    if CLEAN_MIN > 0:
        text += f"\n⏳ <i>This message is removed in {CLEAN_MIN:g} min</i>"
    rows = []
    for it in chunk:
        cap = re.sub(r"\s+", " ", clean_caption(it["caption"])) or "Untitled"
        size = f"{fmt_size(it['size'])} • " if it["size"] else ""
        rows.append([InlineKeyboardButton(f"{ICON.get(it['kind'], '📁')} {size}{cap}"[:60],
                                          callback_data=f"f:{sid}:{it['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg:{sid}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pg:{sid}:{page + 1}"))
        rows.append(nav)
    return text, InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ sending files (bot copies the post from the channel)
async def deliver(context: ContextTypes.DEFAULT_TYPE, chat_id: int, row) -> None:
    cap = clean_caption(row["caption"]) or row["name"] or "Untitled"
    m = await context.bot.copy_message(
        chat_id=chat_id, from_chat_id=row["chat_id"], message_id=row["msg_id"],
        caption=f"<code>{html.escape(cap[:1000])}</code>", parse_mode=ParseMode.HTML,
    )
    if FILE_DELETE_MIN > 0:
        w = await context.bot.send_message(
            chat_id, f"⚠️ <b>Please forward this file now!</b>\nI will delete it in <b>{FILE_DELETE_MIN:g} minutes</b>.",
            parse_mode=ParseMode.HTML, reply_to_message_id=m.message_id)
        schedule_delete(context.bot, chat_id, [m.message_id, w.message_id], FILE_DELETE_MIN)


async def safe_deliver(context, chat_id: int, row) -> None:
    try:
        await deliver(context, chat_id, row)
    except TelegramError as e:
        log.error("copy_message failed (is the bot admin in channel %s?): %s", row["chat_id"], e)
        try:
            await context.bot.send_message(chat_id, "❌ Could not send this file.")
        except TelegramError:
            pass


# ------------------------------------------------------------------ handlers
def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id in OWNERS:
            return await fn(update, context)
    return wrapper


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, u = update.effective_chat, update.effective_user
    if chat.type != "private":
        return  # no /start reply in groups (spam control)
    await db.add_user(u.id, u.first_name or "")
    if context.args and context.args[0].startswith("f_"):  # opened from a group button
        try:
            row = await db.get_file(int(context.args[0][2:]))
        except ValueError:
            row = None
        if row:
            await safe_deliver(context, chat.id, row)
        else:
            await update.message.reply_text("❌ File not found.")
        return
    text = WELCOME.replace("{name}", html.escape(u.first_name or ""))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Support Channel", url=SUPPORT_URL)]]) if SUPPORT_URL else None
    if WELCOME_PHOTO and len(text) <= 1000:
        await update.message.reply_photo(WELCOME_PHOTO, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "Send the name of a movie or series, for example <code>money heist</code>.\n"
        "You can add season, quality or language: <code>money heist s02 720p hindi</code>\n"
        f"⚠️ Files are deleted after {FILE_DELETE_MIN:g} minutes — forward them first.",
        parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not msg.text or not u or u.is_bot:
        return
    q = msg.text.strip()
    private = chat.type == "private"

    if not private:  # groups: approved groups only, short plain text only
        if len(q) > 50 or re.search(r"\n|https?://|t\.me/|@\w", q) or len(q.split()) > 8:
            return
        if not await db.group_ok(chat.id):
            return
    pq = parse_query(q)
    if not pq["words"] or len(norm(q)) < 2:
        return

    now = time.time()  # anti-spam: one search per user every few seconds
    if now - COOL.get(u.id, 0) < COOLDOWN:
        return
    COOL[u.id] = now
    if len(COOL) > 5000:
        COOL.clear()

    status = None
    if private:
        await db.add_user(u.id, u.first_name or "")
        status = await msg.reply_text(f"🔍 Searching <b>{html.escape(q)}</b> …", parse_mode=ParseMode.HTML)
    else:
        await chat.send_action(ChatAction.TYPING)

    try:
        items = await get_results(pq)
    except Exception:
        log.exception("search failed")
        if status:
            await status.edit_text("⚠️ Search is busy right now, please try again.")
        return

    if not items:
        if status:  # groups stay silent when nothing is found
            await status.edit_text(
                f"🔎 <b>{html.escape(q)}</b>\n━━━━━━━━━━━━━━━\n❌ No files found.\n"
                "💡 <i>Check the spelling or try fewer words.</i>", parse_mode=ParseMode.HTML)
            schedule_delete(context.bot, chat.id, [status.message_id], CLEAN_MIN)
        return

    sid = new_session(u.id, u.first_name or "", q, items, chat.type)
    text, kb = render(sid, 0)
    if private:
        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        schedule_delete(context.bot, chat.id, [status.message_id], CLEAN_MIN)
    else:
        sent = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        schedule_delete(context.bot, chat.id, [sent.message_id, msg.message_id], CLEAN_MIN)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    if parts[0] == "noop":
        await q.answer()
        return
    s = get_session(parts[1]) if len(parts) > 1 else None
    if not s:
        await q.answer("Search expired, please search again.", show_alert=True)
        return
    if s["uid"] != q.from_user.id:  # only the person who searched can use the buttons
        await q.answer("🚫 This is not for you!\nSearch your own.", show_alert=True)
        return

    if parts[0] == "pg":
        text, kb = render(parts[1], int(parts[2]))
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        await q.answer()
        return

    if parts[0] == "f":
        row = await db.get_file(int(parts[2]))
        if not row:
            await q.answer("❌ File not found.", show_alert=True)
            return
        chat = q.message.chat
        target = chat.id
        if chat.type != "private" and GROUP_FILES_TO_DM:
            try:
                await context.bot.send_chat_action(q.from_user.id, ChatAction.TYPING)
            except Forbidden:  # user never started the bot -> open it, the file follows
                await q.answer(url=f"https://t.me/{context.bot.username}?start=f_{row['id']}")
                return
            target = q.from_user.id
            await q.answer("📩 Sending to your DM…")
        else:
            await q.answer()
        await safe_deliver(context, target, row)


# ------------------------------------------------------------------ admin commands (OWNER_IDS)
@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await db.stats()
    await update.message.reply_text(
        "📊 Bot Stats\n\n"
        f"👤 Users: {st['users']}\n🎬 Saved files: {st['files']}\n"
        f"🔑 Cached keywords: {st['keywords']}\n👥 Approved groups: {st['groups']}\n"
        f"📡 Channels: {len(CHANNEL_ENTS)} of {len(CHANNELS)} opened")


@owner_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"• {getattr(e, 'title', e)}" for e in CHANNEL_ENTS] or ["(none opened)"]
    await update.message.reply_text("📡 Database channels:\n" + "\n".join(lines))


@owner_only
async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw = norm(" ".join(context.args)) or None
    n = await db.clear_cache(kw)
    await update.message.reply_text(f"🗑 Cleared {n} cached keyword(s). They will be searched again with Telethon.")


@owner_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    if cid > 0:
        await update.message.reply_text("Use /approve inside the group, or /approve -100xxxxxxxxxx")
        return
    await db.add_group(cid)
    await update.message.reply_text(f"✅ Group approved: {cid}")


@owner_only
async def cmd_disapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    await db.del_group(cid)
    await update.message.reply_text(f"🚫 Group removed: {cid}")


@owner_only
async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """save EVERY video/file of all channels in the database (optional, run once)"""
    status = await update.message.reply_text("📚 Indexing all channels… this can take a while.")

    async def run():
        total, last = 0, time.time()
        for ent in CHANNEL_ENTS:
            for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                try:
                    async for m in user.iter_messages(ent, filter=flt):
                        if await index_message(m):
                            total += 1
                        if time.time() - last > 15:
                            last = time.time()
                            try:
                                await status.edit_text(f"📚 Indexing… {total} new files saved")
                            except TelegramError:
                                pass
                except Exception:
                    log.exception("index failed in %s", getattr(ent, "title", ent))
        await status.edit_text(f"✅ Index finished: {total} new files saved.")

    spawn(run())


# ------------------------------------------------------------------ startup
async def post_init(app) -> None:
    global user, CHANNEL_ENTS
    await db.init()
    user = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH, flood_sleep_threshold=60)
    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("TG_SESSION is not valid. Create a new one with gen_session.py")
    me = await user.get_me()
    log.info("Telethon logged in as %s", me.first_name)
    CHANNEL_ENTS = await resolve_channels()
    if CHANNEL_ENTS:  # new videos posted in your channels are saved instantly
        user.add_event_handler(on_new_channel_post, events.NewMessage(chats=CHANNEL_ENTS))
    else:
        log.warning("No channel could be opened. Check CHANNELS and that your account joined them.")


async def post_shutdown(app) -> None:
    if user:
        await user.disconnect()
    await db.close()


def main() -> None:
    app = (ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("clearcache", cmd_clearcache))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("disapprove", cmd_disapprove))
    app.add_handler(CommandHandler("index", cmd_index))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
