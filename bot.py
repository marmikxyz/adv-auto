import asyncio
import html
import logging
import os
import re
import secrets
import time
from functools import wraps

from telegram import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready

import db

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# keep httpx quiet so the bot token inside request URLs is never written to the logs
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ------------------------------------------------------------------ config (env = defaults, /menu commands override)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "").split(",") if c.strip()]  # imported once into the DB
OWNERS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
PER_CHANNEL = int(os.environ.get("PER_CHANNEL_LIMIT", "200"))
RESULT_TTL = int(os.environ.get("RESULT_TTL", "600"))  # seconds a search result list is kept in RAM (not DB)

WELCOME_DEFAULT = (os.environ.get("WELCOME_TEXT") or
    "👋 Hello <b>{name}</b>!\n\nSend me the name of a movie or series and I will find it for you."
).replace("\\n", "\n")

DEFAULTS = {
    "welcome": WELCOME_DEFAULT,
    "welcome_photo": os.environ.get("WELCOME_PHOTO", ""),
    "support_url": os.environ.get("SUPPORT_URL", ""),
    "dm_search": "1",
    "group_search": "1",
    "group_dm": "1" if os.environ.get("GROUP_FILES_TO_DM", "true").lower() == "true" else "0",
    "clean_min": os.environ.get("CLEAN_MIN", "1"),
    "delete_min": os.environ.get("FILE_DELETE_MIN", "10"),
    "store_chat": os.environ.get("STORE_CHANNEL", "").strip(),
}
SET: dict[str, str] = {}  # values changed from inside Telegram (loaded from the DB at startup)


def S(key: str) -> str:
    return SET.get(key, DEFAULTS.get(key, ""))


def on(key: str) -> bool:
    return S(key) == "1"


def fnum(key: str) -> float:
    try:
        return max(0.0, float(S(key)))
    except ValueError:
        return 0.0


async def put(key: str, value: str) -> None:
    SET[key] = value
    await db.set_setting(key, value)


PAGE = 10
MAX_RESULTS = 300
COOLDOWN = 3.0
ICON = {"video": "🎬", "document": "📁", "audio": "🎵"}

user: TelegramClient | None = None  # your Telethon account (created in post_init)
CHANNEL_ENTS: list = []             # source channels (Telethon entities)
RESULT_CACHE: dict[str, tuple[float, list]] = {}  # keyword -> (time, rows)  -- RAM only, nothing is saved
last_store_error = ""

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


# words that end the "name" part: "Ace 2024 1080p Hindi" -> name = "ace"
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
    """best group only: 1) name == keyword / starts with it 2) keyword inside the name
    3) keyword anywhere as a whole word 4) anywhere. Duplicates (same name + size) are dropped."""
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
        # already-saved files come first inside the same tier (they send instantly)
        groups[g].append(((tier, t, d["season"], d["episode"], d["quality"], not r["saved"], -r["msg_id"]), r))
    chosen = next((g for g in groups if g), [])
    chosen.sort(key=lambda x: x[0])
    seen, out = set(), []
    for _, r in chosen:
        key = (r["norm"], r["size"] // 1048576)
        if key in seen:
            continue
        seen.add(key)
        out.append({k: r[k] for k in ("chat_id", "msg_id", "kind", "name", "caption", "size", "saved")})
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


# ------------------------------------------------------------------ searching (Telethon -> RAM only, nothing saved)


def media_kind(m) -> str | None:
    if m.video:
        return "video"
    if m.audio:
        return "audio"
    if m.document:
        return "document"
    return None


def build_row(m) -> dict | None:
    """channel message -> plain dict (NOT written to the database)"""
    kind = media_kind(m)
    if not kind or not m.file:
        return None
    name = clean_caption(m.file.name or "")
    caption = clean_caption(m.message or "") or name or "Untitled"
    n = norm(f"{caption} {name}")
    title = extract_title(norm(caption)) or extract_title(n)
    return {"chat_id": m.chat_id, "msg_id": m.id, "kind": kind, "name": name, "caption": caption,
            "title": title, "norm": n, "size": m.file.size or 0}


_inflight: dict[str, asyncio.Task] = {}


async def telethon_search(kw: str) -> list[dict]:
    sem = asyncio.Semaphore(3)

    async def one(ent) -> list[dict]:
        out = []
        async with sem:
            try:
                async for m in user.iter_messages(ent, search=kw, limit=PER_CHANNEL):
                    r = build_row(m)
                    if r:
                        out.append(r)
            except FloodWaitError as e:
                log.warning("FloodWait %ss on %s", e.seconds, getattr(ent, "title", ent))
            except Exception:
                log.exception("search failed in %s", getattr(ent, "title", ent))
        return out

    res = await asyncio.gather(*(one(e) for e in list(CHANNEL_ENTS)))
    return [r for part in res for r in part]


async def live_rows(kw: str) -> list[dict]:
    c = RESULT_CACHE.get(kw)
    if c and time.time() - c[0] < RESULT_TTL:
        return c[1]
    task = _inflight.get(kw)
    if task is None:  # the same keyword at the same time is searched only once
        task = asyncio.create_task(telethon_search(kw))
        _inflight[kw] = task
        task.add_done_callback(lambda _t: _inflight.pop(kw, None))
    rows = await asyncio.shield(task)
    if rows:
        RESULT_CACHE[kw] = (time.time(), rows)
        while len(RESULT_CACHE) > 200:
            RESULT_CACHE.pop(next(iter(RESULT_CACHE)))
    return rows


def merge(live: list[dict], saved) -> list[dict]:
    out: dict = {}
    for r in live:
        out[(r["chat_id"], r["msg_id"])] = dict(r, saved=False)
    for r in saved:  # a saved file wins over the live copy of the same message
        out[(r["chat_id"], r["msg_id"])] = dict(r, saved=True)
    return list(out.values())


async def get_results(pq: dict) -> list[dict]:
    kw = " ".join(pq["words"])
    live: list[dict] = []
    if user and CHANNEL_ENTS:
        try:
            live = await live_rows(kw)
        except Exception:
            log.exception("telethon search error, using saved files only")
    saved = await db.find_saved(pq["words"])
    return rank(merge(live, saved), pq)


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
    if s and time.time() - s["at"] > 3600:
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
    cm = fnum("clean_min")
    line = "━━━━━━━━━━━━━━━"
    text = f"🔎 <b>{html.escape(s['q'])}</b>\n"
    if in_group:
        text += f'👤 <a href="tg://user?id={s["uid"]}">{html.escape(s["name"])}</a>\n'
    text += f"{line}\n📂 Results: <b>{len(items)}</b> 📄 Page: <b>{page + 1}</b>/<b>{pages}</b>\n{line}\n"
    text += "👇 <i>Tap a file — I will send it to your DM</i>" if in_group and on("group_dm") \
        else "👇 <i>Tap a file to get it</i>"
    if cm > 0:
        text += f"\n⏳ <i>This message is removed in {cm:g} min</i>"
    rows = []
    for i, it in enumerate(chunk, start=page * PAGE):
        cap = re.sub(r"\s+", " ", clean_caption(it["caption"])) or "Untitled"
        size = f"{fmt_size(it['size'])} • " if it["size"] else ""
        rows.append([InlineKeyboardButton(f"{ICON.get(it['kind'], '📁')} {size}{cap}"[:60],
                                          callback_data=f"f:{sid}:{i}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg:{sid}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pg:{sid}:{page + 1}"))
        rows.append(nav)
    return text, InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ saving + sending files
#
#  1st tap on a file : Telethon (your account) forwards it from the source channel into the STORAGE channel,
#                      the file is saved in the database, the bot copies it from the storage channel to the user.
#  next taps         : the bot copies it straight from the storage channel -> Telethon is NOT used.
#  Only files that are really sent get saved.

_locks: dict[tuple, asyncio.Lock] = {}


async def ensure_stored(item, force: bool = False):
    """returns the saved DB row of this file (forwarding it to the storage channel if needed), or None"""
    global last_store_error
    key = (item["chat_id"], item["msg_id"])
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            if not force:
                row = await db.get_saved(*key)
                if row:
                    return row
            store = int(S("store_chat") or 0)
            if not store:
                last_store_error = "No storage channel set. Use /setstore."
                return None
            if not user:
                return None
            store_ent = await user.get_entity(store)
            src = next((e for e in CHANNEL_ENTS if utils.get_peer_id(e) == key[0]), key[0])
            msg = await user.get_messages(src, ids=key[1])
            if not msg or not media_kind(msg):
                last_store_error = "The post no longer exists in the source channel."
                return None
            try:
                fwd = await user.forward_messages(store_ent, msg, drop_author=True)
            except TypeError:  # very old Telethon without drop_author
                fwd = await user.forward_messages(store_ent, msg)
            if isinstance(fwd, list):
                fwd = fwd[0]
            r = build_row(msg)
            last_store_error = ""
            return await db.save_file(r["chat_id"], r["msg_id"], store, fwd.id, r["kind"], r["name"],
                                      r["caption"], r["title"], r["norm"], r["size"])
        except FloodWaitError as e:
            last_store_error = f"FloodWait {e.seconds}s"
            log.warning("FloodWait %ss while storing a file", e.seconds)
        except Exception as e:
            last_store_error = f"{type(e).__name__}: {e}"
            log.exception("could not store file in the storage channel")
        finally:
            _locks.pop(key, None)
    return None


async def deliver(context: ContextTypes.DEFAULT_TYPE, chat_id: int, item) -> None:
    cap = clean_caption(item["caption"]) or item["name"] or "Untitled"
    cap_html = f"<code>{html.escape(cap[:1000])}</code>"

    async def cp(frm: int, mid: int):
        return await context.bot.copy_message(chat_id=chat_id, from_chat_id=frm, message_id=mid,
                                              caption=cap_html, parse_mode=ParseMode.HTML)

    row = await ensure_stored(item)
    if row:
        try:
            m = await cp(row["store_chat_id"], row["store_msg_id"])
        except BadRequest as e:  # the copy in the storage channel was deleted -> store it again once
            log.warning("stored copy missing (%s), storing again", e)
            row = await ensure_stored(item, force=True)
            if not row:
                raise
            m = await cp(row["store_chat_id"], row["store_msg_id"])
    else:  # no storage channel / Telethon problem -> try the source channel directly (bot must be a member)
        m = await cp(item["chat_id"], item["msg_id"])
    dm = fnum("delete_min")
    if dm > 0:
        w = await context.bot.send_message(
            chat_id, f"⚠️ <b>Please forward this file now!</b>\nI will delete it in <b>{dm:g} minutes</b>.",
            parse_mode=ParseMode.HTML, reply_to_message_id=m.message_id)
        schedule_delete(context.bot, chat_id, [m.message_id, w.message_id], dm)


async def safe_deliver(context, chat_id: int, item) -> None:
    try:
        await deliver(context, chat_id, item)
    except Exception as e:
        log.error("sending file failed: %s | store error: %s", e, last_store_error)
        text = "❌ Could not send this file."
        if chat_id in OWNERS:  # owners get the real reason
            text += (f"\n\n<b>Reason:</b> <code>{html.escape(str(e))[:300]}</code>"
                     f"\n<b>Storage:</b> <code>{html.escape(last_store_error or 'ok')[:300]}</code>"
                     "\n\nCheck /setstore (storage channel) and that your Telethon account + the bot are admins there.")
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass


# ------------------------------------------------------------------ user handlers


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
            row = await db.get_saved_by_id(int(context.args[0][2:]))
        except ValueError:
            row = None
        if row:
            await safe_deliver(context, chat.id, row)
        else:
            await update.message.reply_text("❌ File not found.")
        return
    text = S("welcome").replace("{name}", html.escape(u.first_name or ""))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Support Channel", url=S("support_url"))]]) \
        if S("support_url") else None
    photo = S("welcome_photo")
    if photo and len(text) <= 1000:
        await update.message.reply_photo(photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif photo:
        await update.message.reply_photo(photo)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    dm = fnum("delete_min")
    text = ("Send the name of a movie or series, for example <code>money heist</code>.\n"
            "You can add season, quality or language: <code>money heist s02 720p hindi</code>")
    if dm > 0:
        text += f"\n⚠️ Files are deleted after {dm:g} minutes — forward them first."
    if update.effective_user.id in OWNERS:
        text += "\n\n👑 You are an owner — open the control panel with /menu"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, u, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not msg.text or not u or u.is_bot:
        return
    q = msg.text.strip()
    private = chat.type == "private"
    if private:
        if not on("dm_search") and u.id not in OWNERS:
            await msg.reply_text("🔒 Private search is switched off. Please search in our group.")
            return
    else:  # groups: approved groups only, short plain text only
        if not on("group_search"):
            return
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
    cm = fnum("clean_min")
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
            schedule_delete(context.bot, chat.id, [status.message_id], cm)
        return
    sid = new_session(u.id, u.first_name or "", q, items, chat.type)
    text, kb = render(sid, 0)
    if private:
        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        schedule_delete(context.bot, chat.id, [status.message_id], cm)
    else:
        sent = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        schedule_delete(context.bot, chat.id, [sent.message_id, msg.message_id], cm)


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
        try:
            item = s["items"][int(parts[2])]
        except (ValueError, IndexError):
            await q.answer("❌ File not found.", show_alert=True)
            return
        chat = q.message.chat
        if chat.type != "private" and on("group_dm"):
            # ALWAYS open the bot chat from a group: the file is sent after /start with its id
            row = await ensure_stored(item)  # saves the file once (instant if it is already saved)
            if not row:
                msg = "❌ Could not prepare this file."
                if q.from_user.id in OWNERS and last_store_error:
                    msg += f"\n{last_store_error}"
                await q.answer(msg[:190], show_alert=True)
                return
            try:
                await q.answer(url=f"https://t.me/{context.bot.username}?start=f_{row['id']}")
            except TelegramError as e:
                log.warning("answer(url) failed: %s", e)
            return
        await q.answer("📤 Sending…")
        await safe_deliver(context, chat.id, item)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


# ------------------------------------------------------------------ owner: helpers


def parse_onoff(args) -> bool | None:
    if not args:
        return None
    a = args[0].lower()
    if a in ("on", "1", "yes", "enable", "true"):
        return True
    if a in ("off", "0", "no", "disable", "false"):
        return False
    return None


def peer_id(ent) -> int:
    return utils.get_peer_id(ent)


async def resolve_ref(raw: str):
    """@username, t.me/name, t.me/+invite, or -100123… id -> Telethon entity"""
    raw = raw.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", raw)
    if m:
        info = await user(CheckChatInviteRequest(m.group(1)))
        if isinstance(info, ChatInviteAlready):
            return info.chat
        upd = await user(ImportChatInviteRequest(m.group(1)))  # joins the channel with your account
        return upd.chats[0]
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", raw)
    if m:
        raw = m.group(1)
    raw = raw.lstrip("@")
    if raw.lstrip("-").isdigit():
        await user.get_dialogs()  # fills Telethon's cache so numeric ids can be resolved
        return await user.get_entity(int(raw))
    return await user.get_entity(raw)


async def stats_text() -> str:
    st = await db.stats()
    store = S("store_chat") or "not set"
    return ("📊 <b>Bot Stats</b>\n\n"
            f"👤 Users: {st['users']}\n🎬 Saved files: {st['files']}\n"
            f"📡 Source channels: {len(CHANNEL_ENTS)}\n👥 Approved groups: {st['groups']}\n"
            f"🗄 Storage channel: <code>{html.escape(store)}</code>")


# ------------------------------------------------------------------ owner: control panel (/menu)

TOGGLES = {"dm_search": "DM search", "group_search": "Group search", "group_dm": "Group files → DM"}

OWNER_COMMANDS = [
    ("menu", "Admin control panel"),
    ("setphoto", "Set welcome photo (reply to a photo)"),
    ("delphoto", "Remove welcome photo"),
    ("setwelcome", "Set welcome text ({name} = user name)"),
    ("resetwelcome", "Reset welcome text to default"),
    ("setsupport", "Set support channel link (or off)"),
    ("addsource", "Add a source channel manually"),
    ("delsource", "Remove a source channel"),
    ("sources", "List source channels"),
    ("setstore", "Set the storage channel"),
    ("dmsearch", "DM search on/off"),
    ("groupsearch", "Group search on/off"),
    ("groupdm", "Send group files to DM on/off"),
    ("approve", "Approve a group"),
    ("disapprove", "Remove a group"),
    ("groups", "List approved groups"),
    ("broadcast", "Broadcast to users / groups / all"),
    ("setclean", "Result list auto-delete minutes (0 = off)"),
    ("setdelete", "Sent file auto-delete minutes (0 = off)"),
    ("stats", "Bot statistics"),
    ("clearcache", "Clear the search result cache"),
    ("help", "Help"),
]
USER_COMMANDS = [("start", "Start the bot"), ("help", "How to search")]


def panel():
    def btn(key):
        return InlineKeyboardButton(f"{'✅' if on(key) else '❌'} {TOGGLES[key]}", callback_data=f"adm:t:{key}")

    text = ("🛠 <b>Control Panel</b>\n\n"
            f"🔎 DM search: <b>{'ON' if on('dm_search') else 'OFF'}</b>\n"
            f"👥 Group search: <b>{'ON' if on('group_search') else 'OFF'}</b>\n"
            f"📩 Group files → DM: <b>{'ON' if on('group_dm') else 'OFF'}</b>\n"
            f"📡 Source channels: <b>{len(CHANNEL_ENTS)}</b>\n"
            f"🗄 Storage channel: <b>{'set' if S('store_chat') else 'NOT SET'}</b>\n"
            f"🖼 Welcome photo: <b>{'set' if S('welcome_photo') else 'none'}</b>\n"
            f"⏳ Result list delete: <b>{fnum('clean_min'):g} min</b> | File delete: <b>{fnum('delete_min'):g} min</b>")
    kb = InlineKeyboardMarkup([
        [btn("dm_search"), btn("group_search")],
        [btn("group_dm")],
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats"),
         InlineKeyboardButton("📡 Sources", callback_data="adm:sources")],
        [InlineKeyboardButton("👥 Groups", callback_data="adm:groups"),
         InlineKeyboardButton("📜 All commands", callback_data="adm:cmds")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh")],
    ])
    return text, kb


@owner_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = panel()
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def sources_text() -> str:
    lines = [f"{i}. {html.escape(getattr(e, 'title', str(e)))}  <code>{peer_id(e)}</code>"
             for i, e in enumerate(CHANNEL_ENTS, 1)]
    return "📡 <b>Source channels</b>\n\n" + ("\n".join(lines) if lines else "(none) — add one with /addsource")


async def groups_text(bot) -> str:
    lines = []
    for cid in await db.all_groups():
        try:
            title = (await bot.get_chat(cid)).title or ""
        except TelegramError:
            title = ""
        lines.append(f"• {html.escape(title)} <code>{cid}</code>")
    return "👥 <b>Approved groups</b>\n\n" + ("\n".join(lines) if lines else "(none) — use /approve inside a group")


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in OWNERS:
        await q.answer("Owners only.", show_alert=True)
        return
    parts = q.data.split(":")
    action = parts[1]
    if action == "t" and parts[2] in TOGGLES:
        await put(parts[2], "0" if on(parts[2]) else "1")
        action = "refresh"
    if action == "refresh":
        text, kb = panel()
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass
        await q.answer("Updated")
        return
    await q.answer()
    m = q.message
    if action == "stats":
        await m.reply_text(await stats_text(), parse_mode=ParseMode.HTML)
    elif action == "sources":
        await m.reply_text(sources_text(), parse_mode=ParseMode.HTML)
    elif action == "groups":
        await m.reply_text(await groups_text(context.bot), parse_mode=ParseMode.HTML)
    elif action == "cmds":
        await m.reply_text("📜 <b>Commands</b>\n\n" + "\n".join(f"/{c} — {d}" for c, d in OWNER_COMMANDS),
                           parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ owner: welcome / photo / support


@owner_only
async def cmd_setphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    photos = msg.photo or (msg.reply_to_message.photo if msg.reply_to_message else None)
    if not photos:
        await msg.reply_text("Reply to a photo with /setphoto, or send a photo with the caption /setphoto")
        return
    await put("welcome_photo", photos[-1].file_id)
    await msg.reply_text("🖼 Welcome photo saved.")


@owner_only
async def cmd_delphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await put("welcome_photo", "")
    await update.effective_message.reply_text("🗑 Welcome photo removed.")


@owner_only
async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.reply_to_message and (msg.reply_to_message.text or msg.reply_to_message.caption):
        text = msg.reply_to_message.text_html or msg.reply_to_message.caption_html
    else:
        parts = (msg.text_html or "").split(None, 1)
        text = parts[1] if len(parts) > 1 else ""
    if not text:
        await msg.reply_text("Usage: /setwelcome <text>  (or reply to a text with /setwelcome)\n"
                             "Use {name} for the user's name. HTML like <b>bold</b> works.")
        return
    await put("welcome", text)
    await msg.reply_text("📝 Welcome text saved. Preview:")
    await msg.reply_text(text.replace("{name}", html.escape(update.effective_user.first_name or "")),
                         parse_mode=ParseMode.HTML)


@owner_only
async def cmd_resetwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await put("welcome", WELCOME_DEFAULT)
    await update.effective_message.reply_text("♻️ Welcome text reset to default.")


@owner_only
async def cmd_setsupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /setsupport https://t.me/yourchannel   (or /setsupport off)")
        return
    val = context.args[0]
    if val.lower() == "off":
        await put("support_url", "")
        await update.effective_message.reply_text("Support button removed.")
        return
    if not val.startswith("http"):
        val = "https://t.me/" + val.lstrip("@")
    await put("support_url", val)
    await update.effective_message.reply_text(f"✅ Support link saved: {val}")


# ------------------------------------------------------------------ owner: source channels / storage channel


@owner_only
async def cmd_addsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /addsource @channel | t.me/channel | t.me/+invite | -100123456789\n"
                             "Your Telethon account must be able to open it (private invite links are joined for you).")
        return
    ref = " ".join(context.args)
    try:
        ent = await resolve_ref(ref)
    except Exception as e:
        await msg.reply_text(f"❌ Cannot open that channel: {e}")
        return
    cid = peer_id(ent)
    if any(peer_id(e) == cid for e in CHANNEL_ENTS):
        await msg.reply_text("Already added.")
        return
    title = getattr(ent, "title", None) or ref
    await db.add_source(cid, title, ref)
    CHANNEL_ENTS.append(ent)
    RESULT_CACHE.clear()
    await msg.reply_text(f"✅ Source added: {title}\nTotal sources: {len(CHANNEL_ENTS)}")


@owner_only
async def cmd_delsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /delsource <number from /sources> | <channel id> | @username")
        return
    arg = context.args[0].strip()
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(CHANNEL_ENTS):
        target = CHANNEL_ENTS[int(arg) - 1]
    else:
        for e in CHANNEL_ENTS:
            if arg.lstrip("-").isdigit() and peer_id(e) == int(arg):
                target = e
            elif getattr(e, "username", None) and e.username.lower() == arg.lstrip("@").lower():
                target = e
    if not target:
        await msg.reply_text("Not found. See /sources")
        return
    CHANNEL_ENTS.remove(target)
    await db.del_source(peer_id(target))
    RESULT_CACHE.clear()
    await msg.reply_text(f"🗑 Removed: {getattr(target, 'title', arg)}")


@owner_only
async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(sources_text(), parse_mode=ParseMode.HTML)


@owner_only
async def cmd_setstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """storage channel: your Telethon account forwards files here, the bot copies them to users"""
    msg = update.effective_message
    if not context.args:
        cur = S("store_chat") or "not set"
        await msg.reply_text(
            f"🗄 Storage channel: <code>{html.escape(cur)}</code>\n\n"
            "Create a private channel, add <b>the bot</b> and <b>your Telethon account</b> as admins "
            "(allow posting), then send:\n<code>/setstore -100xxxxxxxxxx</code>",
            parse_mode=ParseMode.HTML)
        return
    try:
        ent = await resolve_ref(" ".join(context.args))
    except Exception as e:
        await msg.reply_text(f"❌ Telethon cannot open that channel: {e}")
        return
    cid = peer_id(ent)
    warn = ""
    try:
        t = await context.bot.send_message(cid, "✅ Storage channel connected.")
        await context.bot.delete_message(cid, t.message_id)
    except TelegramError as e:
        warn = f"\n⚠️ The bot cannot post there ({e}). Make the bot an admin of that channel."
    await put("store_chat", str(cid))
    await msg.reply_text(f"✅ Storage channel saved: {getattr(ent, 'title', cid)} (<code>{cid}</code>){warn}",
                         parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ owner: toggles / timers / groups / stats


async def _toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    v = parse_onoff(context.args)
    if v is None:
        v = not on(key)  # no argument -> flip
    await put(key, "1" if v else "0")
    await update.effective_message.reply_text(f"{TOGGLES[key]}: {'ON ✅' if v else 'OFF ❌'}")


@owner_only
async def cmd_dmsearch(update, context):
    await _toggle(update, context, "dm_search")


@owner_only
async def cmd_groupsearch(update, context):
    await _toggle(update, context, "group_search")


@owner_only
async def cmd_groupdm(update, context):
    await _toggle(update, context, "group_dm")


async def _set_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, label: str):
    try:
        v = max(0.0, float(context.args[0]))
    except (IndexError, ValueError):
        await update.effective_message.reply_text(f"Usage: /{'setclean' if key == 'clean_min' else 'setdelete'} <minutes>  (0 = never)")
        return
    await put(key, str(v))
    await update.effective_message.reply_text(f"⏳ {label}: {v:g} min" if v else f"⏳ {label}: off")


@owner_only
async def cmd_setclean(update, context):
    await _set_minutes(update, context, "clean_min", "Result list auto-delete")


@owner_only
async def cmd_setdelete(update, context):
    await _set_minutes(update, context, "delete_min", "File auto-delete")


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(await stats_text(), parse_mode=ParseMode.HTML)


@owner_only
async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = len(RESULT_CACHE)
    RESULT_CACHE.clear()
    await update.effective_message.reply_text(f"🗑 Cleared {n} cached search(es). Saved files are untouched.")


@owner_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    if cid > 0:
        await update.effective_message.reply_text("Use /approve inside the group, or /approve -100xxxxxxxxxx")
        return
    await db.add_group(cid)
    await update.effective_message.reply_text(f"✅ Group approved: {cid}")


@owner_only
async def cmd_disapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else update.effective_chat.id
    await db.del_group(cid)
    await update.effective_message.reply_text(f"🚫 Group removed: {cid}")


@owner_only
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(await groups_text(context.bot), parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ owner: broadcast

TARGETS = ("users", "groups", "all")


@owner_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast [users|groups|all] <text>     or reply to any message with /broadcast [target]"""
    msg = update.effective_message
    parts = (msg.text_html or "").split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    target = "users"
    first = rest.split(None, 1)[0].lower() if rest else ""
    if first in TARGETS:
        target = first
        rest = rest[len(first):].strip()
    reply = msg.reply_to_message
    if not reply and not rest:
        await msg.reply_text("Usage:\n/broadcast <text>  → all users\n/broadcast groups <text>\n/broadcast all <text>\n"
                             "Or reply to any message (photo, video, text…) with /broadcast [users|groups|all]")
        return
    targets: list[tuple[int, str]] = []
    if target in ("users", "all"):
        targets += [(i, "user") for i in await db.all_users()]
    if target in ("groups", "all"):
        targets += [(i, "group") for i in await db.all_groups()]
    if not targets:
        await msg.reply_text("Nobody to send to.")
        return
    status = await msg.reply_text(f"📣 Broadcasting to {len(targets)} chat(s)…")

    async def run():
        ok = fail = 0
        for n, (cid, kind) in enumerate(targets, 1):
            for _ in range(2):
                try:
                    if reply:
                        await context.bot.copy_message(cid, reply.chat_id, reply.message_id)
                    else:
                        await context.bot.send_message(cid, rest, parse_mode=ParseMode.HTML)
                    ok += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Forbidden:
                    fail += 1
                    if kind == "user":  # blocked the bot -> forget the user
                        await db.del_user(cid)
                    break
                except TelegramError:
                    fail += 1
                    break
            else:
                fail += 1
            await asyncio.sleep(0.05)
            if n % 50 == 0:
                try:
                    await status.edit_text(f"📣 Broadcasting… {n}/{len(targets)}")
                except TelegramError:
                    pass
        try:
            await status.edit_text(f"✅ Broadcast finished.\nSent: {ok}\nFailed: {fail}")
        except TelegramError:
            pass

    spawn(run())


# ------------------------------------------------------------------ startup


async def load_sources() -> None:
    await user.get_dialogs()  # fills Telethon's cache so numeric ids can be resolved
    if SET.get("env_imported") != "1":  # CHANNELS from the env are imported once, then the DB is the master list
        for raw in CHANNELS:
            try:
                ent = await resolve_ref(raw)
                await db.add_source(peer_id(ent), getattr(ent, "title", raw), raw)
            except Exception as e:
                log.error("cannot open channel %s: %s (is your account a member?)", raw, e)
        await put("env_imported", "1")
    CHANNEL_ENTS.clear()
    for r in await db.list_sources():
        try:
            ent = await user.get_entity(r["chat_id"])
        except Exception:
            try:
                ent = await resolve_ref(r["ref"])
            except Exception as e:
                log.error("cannot open source %s (%s): %s", r["title"], r["chat_id"], e)
                continue
        CHANNEL_ENTS.append(ent)
        log.info("source ok: %s", getattr(ent, "title", r["chat_id"]))
    if not CHANNEL_ENTS:
        log.warning("No source channel yet. Add one with /addsource")


async def post_init(app) -> None:
    global user
    await db.init()
    SET.update(await db.load_settings())
    user = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH, flood_sleep_threshold=60)
    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("TG_SESSION is not valid. Create a new one with gen_session.py")
    me = await user.get_me()
    log.info("Telethon logged in as %s", me.first_name)
    await load_sources()
    # command menu: normal users see /start /help, owners see everything
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in USER_COMMANDS], scope=BotCommandScopeDefault())
    except TelegramError:
        log.exception("set_my_commands (default) failed")
    for oid in OWNERS:
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in OWNER_COMMANDS],
                                          scope=BotCommandScopeChat(oid))
        except TelegramError as e:
            log.warning("owner menu for %s not set (open the bot and press /start first): %s", oid, e)


async def post_shutdown(app) -> None:
    if user:
        await user.disconnect()
    await db.close()


def main() -> None:
    app = (ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    cmds = {
        "start": cmd_start, "help": cmd_help, "menu": cmd_menu,
        "setphoto": cmd_setphoto, "delphoto": cmd_delphoto,
        "setwelcome": cmd_setwelcome, "resetwelcome": cmd_resetwelcome, "setsupport": cmd_setsupport,
        "addsource": cmd_addsource, "delsource": cmd_delsource, "sources": cmd_sources, "setstore": cmd_setstore,
        "dmsearch": cmd_dmsearch, "groupsearch": cmd_groupsearch, "groupdm": cmd_groupdm,
        "approve": cmd_approve, "disapprove": cmd_disapprove, "groups": cmd_groups,
        "broadcast": cmd_broadcast, "setclean": cmd_setclean, "setdelete": cmd_setdelete,
        "stats": cmd_stats, "clearcache": cmd_clearcache,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    # photo sent together with the caption /setphoto
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/setphoto"), cmd_setphoto))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(pg|f|noop)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
