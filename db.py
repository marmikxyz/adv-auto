import os

import asyncpg

pool: asyncpg.Pool | None = None

# NOTE: the old `files` and `searches` tables are no longer used.
# You can delete them:  DROP TABLE files; DROP TABLE searches;
SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_files (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       BIGINT NOT NULL,          -- source channel
    msg_id        BIGINT NOT NULL,          -- message id in the source channel
    store_chat_id BIGINT NOT NULL,          -- storage channel (bot is admin there)
    store_msg_id  BIGINT NOT NULL,          -- copy of the file inside the storage channel
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    norm          TEXT NOT NULL DEFAULT '',
    size          BIGINT NOT NULL DEFAULT 0,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sources (
    chat_id  BIGINT PRIMARY KEY,
    title    TEXT NOT NULL DEFAULT '',
    ref      TEXT NOT NULL DEFAULT '',
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS users (
    user_id   BIGINT PRIMARY KEY,
    name      TEXT,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS groups (
    chat_id BIGINT PRIMARY KEY
);
"""


async def init() -> None:
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    await pool.execute(SCHEMA)
    try:  # makes ILIKE '%word%' fast on big libraries (optional)
        await pool.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await pool.execute(
            "CREATE INDEX IF NOT EXISTS saved_norm_trgm ON saved_files USING gin (norm gin_trgm_ops)"
        )
    except Exception:
        pass


async def close() -> None:
    if pool:
        await pool.close()


# ---------- saved files (only files that were really sent) ----------

async def save_file(chat_id, msg_id, store_chat_id, store_msg_id, kind, name, caption, title, norm, size):
    return await pool.fetchrow(
        """INSERT INTO saved_files
               (chat_id, msg_id, store_chat_id, store_msg_id, kind, name, caption, title, norm, size)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
           ON CONFLICT (chat_id, msg_id) DO UPDATE SET
               store_chat_id = EXCLUDED.store_chat_id,
               store_msg_id  = EXCLUDED.store_msg_id,
               kind = EXCLUDED.kind, name = EXCLUDED.name, caption = EXCLUDED.caption,
               title = EXCLUDED.title, norm = EXCLUDED.norm, size = EXCLUDED.size
           RETURNING *""",
        chat_id, msg_id, store_chat_id, store_msg_id, kind, name, caption, title, norm, size,
    )


async def get_saved(chat_id: int, msg_id: int):
    return await pool.fetchrow(
        "SELECT * FROM saved_files WHERE chat_id = $1 AND msg_id = $2", chat_id, msg_id
    )


async def get_saved_by_id(file_id: int):
    return await pool.fetchrow("SELECT * FROM saved_files WHERE id = $1", file_id)


async def find_saved(words: list[str], limit: int = 3000):
    patterns = [f"%{w}%" for w in words]
    return await pool.fetch(
        """SELECT id, chat_id, msg_id, kind, name, caption, title, norm, size
           FROM saved_files WHERE norm ILIKE ALL($1::text[]) LIMIT $2""",
        patterns, limit,
    )


# ---------- settings ----------

async def load_settings() -> dict:
    rows = await pool.fetch("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


async def set_setting(key: str, value: str) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = $2", key, value,
    )


async def del_setting(key: str) -> None:
    await pool.execute("DELETE FROM settings WHERE key = $1", key)


# ---------- source channels ----------

async def add_source(chat_id: int, title: str, ref: str) -> None:
    await pool.execute(
        "INSERT INTO sources (chat_id, title, ref) VALUES ($1,$2,$3) "
        "ON CONFLICT (chat_id) DO UPDATE SET title = $2, ref = $3", chat_id, title, ref,
    )


async def del_source(chat_id: int) -> None:
    await pool.execute("DELETE FROM sources WHERE chat_id = $1", chat_id)


async def list_sources():
    return await pool.fetch("SELECT chat_id, title, ref FROM sources ORDER BY added_at")


# ---------- users / groups ----------

async def add_user(user_id: int, name: str) -> None:
    await pool.execute(
        "INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, name
    )


async def del_user(user_id: int) -> None:
    await pool.execute("DELETE FROM users WHERE user_id = $1", user_id)


async def all_users() -> list[int]:
    return [r["user_id"] for r in await pool.fetch("SELECT user_id FROM users")]


async def group_ok(chat_id: int) -> bool:
    return bool(await pool.fetchval("SELECT 1 FROM groups WHERE chat_id = $1", chat_id))


async def add_group(chat_id: int) -> None:
    await pool.execute("INSERT INTO groups (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)


async def del_group(chat_id: int) -> None:
    await pool.execute("DELETE FROM groups WHERE chat_id = $1", chat_id)


async def all_groups() -> list[int]:
    return [r["chat_id"] for r in await pool.fetch("SELECT chat_id FROM groups")]


async def stats() -> dict:
    return {
        "users": await pool.fetchval("SELECT count(*) FROM users"),
        "files": await pool.fetchval("SELECT count(*) FROM saved_files"),
        "sources": await pool.fetchval("SELECT count(*) FROM sources"),
        "groups": await pool.fetchval("SELECT count(*) FROM groups"),
    }
