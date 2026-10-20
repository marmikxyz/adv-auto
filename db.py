import os

import asyncpg

pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id        BIGSERIAL PRIMARY KEY,
    chat_id   BIGINT NOT NULL,
    msg_id    BIGINT NOT NULL,
    kind      TEXT   NOT NULL,
    name      TEXT   NOT NULL DEFAULT '',
    caption   TEXT   NOT NULL DEFAULT '',
    title     TEXT   NOT NULL DEFAULT '',
    norm      TEXT   NOT NULL DEFAULT '',
    size      BIGINT NOT NULL DEFAULT 0,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS searches (
    keyword     TEXT PRIMARY KEY,
    searched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    found       INT NOT NULL DEFAULT 0
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
            "CREATE INDEX IF NOT EXISTS files_norm_trgm ON files USING gin (norm gin_trgm_ops)"
        )
    except Exception:
        pass


async def close() -> None:
    if pool:
        await pool.close()


# ---------- files ----------
async def upsert_file(chat_id, msg_id, kind, name, caption, title, norm, size) -> bool:
    """True if the file is new."""
    row = await pool.fetchrow(
        """INSERT INTO files (chat_id, msg_id, kind, name, caption, title, norm, size)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (chat_id, msg_id) DO NOTHING RETURNING id""",
        chat_id, msg_id, kind, name, caption, title, norm, size,
    )
    return row is not None


async def find_candidates(words: list[str], limit: int = 3000):
    patterns = [f"%{w}%" for w in words]
    return await pool.fetch(
        """SELECT id, chat_id, msg_id, kind, name, caption, title, norm, size
           FROM files WHERE norm ILIKE ALL($1::text[]) LIMIT $2""",
        patterns, limit,
    )


async def get_file(file_id: int):
    return await pool.fetchrow("SELECT * FROM files WHERE id = $1", file_id)


# ---------- keyword cache ----------
async def keyword_fresh(keyword: str, days: int) -> bool:
    row = await pool.fetchval(
        "SELECT 1 FROM searches WHERE keyword = $1 AND searched_at > now() - make_interval(days => $2)",
        keyword, days,
    )
    return bool(row)


async def mark_keyword(keyword: str, found: int) -> None:
    await pool.execute(
        """INSERT INTO searches (keyword, found) VALUES ($1, $2)
           ON CONFLICT (keyword) DO UPDATE SET searched_at = now(), found = $2""",
        keyword, found,
    )


async def clear_cache(keyword: str | None = None) -> int:
    if keyword:
        res = await pool.execute("DELETE FROM searches WHERE keyword = $1", keyword)
    else:
        res = await pool.execute("DELETE FROM searches")
    return int(res.split()[-1])


# ---------- users / groups ----------
async def add_user(user_id: int, name: str) -> None:
    await pool.execute(
        "INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, name
    )


async def group_ok(chat_id: int) -> bool:
    return bool(await pool.fetchval("SELECT 1 FROM groups WHERE chat_id = $1", chat_id))


async def add_group(chat_id: int) -> None:
    await pool.execute("INSERT INTO groups (chat_id) VALUES ($1) ON CONFLICT DO NOTHING", chat_id)


async def del_group(chat_id: int) -> None:
    await pool.execute("DELETE FROM groups WHERE chat_id = $1", chat_id)


async def stats() -> dict:
    return {
        "users": await pool.fetchval("SELECT count(*) FROM users"),
        "files": await pool.fetchval("SELECT count(*) FROM files"),
        "keywords": await pool.fetchval("SELECT count(*) FROM searches"),
        "groups": await pool.fetchval("SELECT count(*) FROM groups"),
    }
