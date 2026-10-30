"""
Database layer for bot.py  (PostgreSQL via asyncpg, uses DATABASE_URL)

Self-healing schema: if a table with the same name already exists in your database (from an older
version / another project) it is checked at startup:
  - all key columns present  -> missing columns are added, the table is reused
  - key column missing       -> a fresh table named  af_<name>  is used instead
                                (old user ids are copied over when possible), the old table is never touched.
"""
import logging
import os
import ssl

import asyncpg

log = logging.getLogger("db")

pool: asyncpg.Pool | None = None

# real table names (changed to af_<name> by init() when an incompatible table already exists)
T_SETTINGS = "settings"
T_USERS = "users"
T_GROUPS = "groups"
T_SOURCES = "sources"
T_FILES = "files"

CREATE = {
    "settings": """CREATE TABLE IF NOT EXISTS "{t}" (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )""",
    "users": """CREATE TABLE IF NOT EXISTS "{t}" (
        id         BIGINT PRIMARY KEY,
        first_name TEXT NOT NULL DEFAULT '',
        joined_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "groups": """CREATE TABLE IF NOT EXISTS "{t}" (
        chat_id BIGINT PRIMARY KEY
    )""",
    "sources": """CREATE TABLE IF NOT EXISTS "{t}" (
        chat_id BIGINT PRIMARY KEY,
        title   TEXT NOT NULL DEFAULT '',
        ref     TEXT NOT NULL DEFAULT ''
    )""",
    "files": """CREATE TABLE IF NOT EXISTS "{t}" (
        id            SERIAL PRIMARY KEY,
        chat_id       BIGINT NOT NULL,
        msg_id        BIGINT NOT NULL,
        store_chat_id BIGINT NOT NULL,
        store_msg_id  BIGINT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'document',
        name          TEXT NOT NULL DEFAULT '',
        caption       TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        norm          TEXT NOT NULL DEFAULT '',
        size          BIGINT NOT NULL DEFAULT 0,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (chat_id, msg_id)
    )""",
}

# columns that MUST already exist for an old table to be reused
KEYS = {
    "settings": ["key"],
    "users": ["id"],
    "groups": ["chat_id"],
    "sources": ["chat_id"],
    "files": ["id", "chat_id", "msg_id", "store_chat_id", "store_msg_id"],
}
# columns added when missing
EXTRA = {
    "settings": {"value": "TEXT NOT NULL DEFAULT ''"},
    "users": {"first_name": "TEXT NOT NULL DEFAULT ''", "joined_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"},
    "groups": {},
    "sources": {"title": "TEXT NOT NULL DEFAULT ''", "ref": "TEXT NOT NULL DEFAULT ''"},
    "files": {"kind": "TEXT NOT NULL DEFAULT 'document'", "name": "TEXT NOT NULL DEFAULT ''",
              "caption": "TEXT NOT NULL DEFAULT ''", "title": "TEXT NOT NULL DEFAULT ''",
              "norm": "TEXT NOT NULL DEFAULT ''", "size": "BIGINT NOT NULL DEFAULT 0",
              "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()"},
}
# unique index needed by ON CONFLICT (...)
UNIQUE = {
    "settings": ["key"],
    "users": ["id"],
    "groups": ["chat_id"],
    "sources": ["chat_id"],
    "files": ["chat_id", "msg_id"],
}
# old column names that may hold the telegram user id in an older users table
OLD_USER_COLS = ("user_id", "uid", "tg_id", "telegram_id", "chat_id", "userid")


def _ssl_ctx(dsn: str):
    """Heroku Postgres needs SSL but uses a self-signed certificate"""
    if "localhost" in dsn or "127.0.0.1" in dsn:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


async def _columns(conn, table: str) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1", table)
    return {r["column_name"]: r["data_type"] for r in rows}


async def _prepare(conn, base: str) -> str:
    """make sure a usable table for `base` exists and return its real name"""
    cols = await _columns(conn, base)
    if not cols:
        await conn.execute(CREATE[base].format(t=base))
        return base
    if all(k in cols for k in KEYS[base]):
        try:
            async with conn.transaction():
                for c, ddl in EXTRA[base].items():
                    if c not in cols:
                        await conn.execute(f'ALTER TABLE "{base}" ADD COLUMN IF NOT EXISTS {c} {ddl}')
                u = UNIQUE[base]
                await conn.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS "{base}_{"_".join(u)}_uq" ON "{base}" ({", ".join(u)})')
            return base
        except Exception:
            log.exception("could not upgrade existing table %s", base)
    alt = "af_" + base
    log.warning("table %s exists with a different layout (%s) -> using %s instead",
                base, ", ".join(cols), alt)
    await conn.execute(CREATE[alt.removeprefix("af_")].format(t=alt))
    return alt


async def init() -> None:
    global pool, T_SETTINGS, T_USERS, T_GROUPS, T_SOURCES, T_FILES
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set. Add the Heroku Postgres add-on.")
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, ssl=_ssl_ctx(dsn))
    async with pool.acquire() as c:
        T_SETTINGS = await _prepare(c, "settings")
        T_USERS = await _prepare(c, "users")
        T_GROUPS = await _prepare(c, "groups")
        T_SOURCES = await _prepare(c, "sources")
        T_FILES = await _prepare(c, "files")
        await c.execute(f'CREATE INDEX IF NOT EXISTS "{T_FILES}_norm_idx" ON "{T_FILES}" (norm)')
        # old users table with another layout -> keep its user ids so broadcasts still reach them
        if T_USERS != "users":
            old = await _columns(c, "users")
            col = next((x for x in OLD_USER_COLS if x in old and "int" in old[x]), None)
            if col:
                try:
                    n = await c.execute(
                        f'INSERT INTO "{T_USERS}" (id) SELECT "{col}" FROM "users" '
                        f'WHERE "{col}" IS NOT NULL ON CONFLICT DO NOTHING')
                    log.info("copied old users (%s): %s", col, n)
                except Exception:
                    log.exception("could not copy old users")
    log.info("database ready (tables: %s, %s, %s, %s, %s)", T_SETTINGS, T_USERS, T_GROUPS, T_SOURCES, T_FILES)


async def close() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


# ------------------------------------------------------------------ settings


async def load_settings() -> dict[str, str]:
    rows = await pool.fetch(f'SELECT key, value FROM "{T_SETTINGS}"')
    return {r["key"]: r["value"] for r in rows}


async def set_setting(key: str, value: str) -> None:
    await pool.execute(
        f'INSERT INTO "{T_SETTINGS}" (key, value) VALUES ($1, $2) '
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value)


# ------------------------------------------------------------------ users


async def add_user(uid: int, first_name: str = "") -> None:
    await pool.execute(
        f'INSERT INTO "{T_USERS}" (id, first_name) VALUES ($1, $2) '
        "ON CONFLICT (id) DO UPDATE SET first_name = EXCLUDED.first_name",
        uid, first_name or "")


async def all_users() -> list[int]:
    return [r["id"] for r in await pool.fetch(f'SELECT id FROM "{T_USERS}"')]


async def del_user(uid: int) -> None:
    await pool.execute(f'DELETE FROM "{T_USERS}" WHERE id = $1', uid)


# ------------------------------------------------------------------ approved groups


async def add_group(chat_id: int) -> None:
    await pool.execute(f'INSERT INTO "{T_GROUPS}" (chat_id) VALUES ($1) ON CONFLICT DO NOTHING', chat_id)


async def del_group(chat_id: int) -> None:
    await pool.execute(f'DELETE FROM "{T_GROUPS}" WHERE chat_id = $1', chat_id)


async def all_groups() -> list[int]:
    return [r["chat_id"] for r in await pool.fetch(f'SELECT chat_id FROM "{T_GROUPS}"')]


async def group_ok(chat_id: int) -> bool:
    return await pool.fetchval(f'SELECT 1 FROM "{T_GROUPS}" WHERE chat_id = $1', chat_id) is not None


# ------------------------------------------------------------------ source channels


async def add_source(chat_id: int, title: str, ref: str) -> None:
    await pool.execute(
        f'INSERT INTO "{T_SOURCES}" (chat_id, title, ref) VALUES ($1, $2, $3) '
        "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, ref = EXCLUDED.ref",
        chat_id, title or "", ref or "")


async def del_source(chat_id: int) -> None:
    await pool.execute(f'DELETE FROM "{T_SOURCES}" WHERE chat_id = $1', chat_id)


async def list_sources() -> list[dict]:
    rows = await pool.fetch(f'SELECT chat_id, title, ref FROM "{T_SOURCES}" ORDER BY chat_id')
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ saved files


async def find_saved(words: list[str]) -> list[dict]:
    """saved files whose normalised text contains every word"""
    if not words:
        return []
    conds = " AND ".join(f"norm ILIKE ${i}" for i in range(1, len(words) + 1))
    params = [f"%{w}%" for w in words]
    rows = await pool.fetch(
        f'SELECT chat_id, msg_id, kind, name, caption, title, norm, size '
        f'FROM "{T_FILES}" WHERE {conds} LIMIT 300', *params)
    return [dict(r) for r in rows]


async def get_saved(chat_id: int, msg_id: int) -> dict | None:
    return _row(await pool.fetchrow(
        f'SELECT * FROM "{T_FILES}" WHERE chat_id = $1 AND msg_id = $2', chat_id, msg_id))


async def get_saved_by_id(file_id: int) -> dict | None:
    return _row(await pool.fetchrow(f'SELECT * FROM "{T_FILES}" WHERE id = $1', file_id))


async def save_file(chat_id: int, msg_id: int, store_chat_id: int, store_msg_id: int, kind: str,
                    name: str, caption: str, title: str, norm: str, size: int) -> dict:
    row = await pool.fetchrow(
        f'INSERT INTO "{T_FILES}" (chat_id, msg_id, store_chat_id, store_msg_id, kind, name, caption, title, norm, size) '
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (chat_id, msg_id) DO UPDATE SET "
        "store_chat_id = EXCLUDED.store_chat_id, store_msg_id = EXCLUDED.store_msg_id, "
        "kind = EXCLUDED.kind, name = EXCLUDED.name, caption = EXCLUDED.caption, "
        "title = EXCLUDED.title, norm = EXCLUDED.norm, size = EXCLUDED.size "
        "RETURNING *",
        chat_id, msg_id, store_chat_id, store_msg_id, kind, name, caption, title, norm, size or 0)
    return dict(row)


# ------------------------------------------------------------------ stats


async def stats() -> dict:
    return {
        "users": await pool.fetchval(f'SELECT count(*) FROM "{T_USERS}"'),
        "files": await pool.fetchval(f'SELECT count(*) FROM "{T_FILES}"'),
        "groups": await pool.fetchval(f'SELECT count(*) FROM "{T_GROUPS}"'),
    }
