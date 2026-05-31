"""
Слой работы с базой данных (SQLite + aiosqlite).
Все функции асинхронные.
"""

import os
import aiosqlite
from datetime import date, timedelta

# На Railway: DB_PATH=/data/gym.db через Variables + Volume на /data
DB_PATH = os.environ.get("DB_PATH", "gym.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id            INTEGER UNIQUE NOT NULL,
                username         TEXT,
                name             TEXT NOT NULL,
                cycle_day        INTEGER DEFAULT 1,
                cycle_start_date TEXT
            );
            CREATE TABLE IF NOT EXISTS workouts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                date      TEXT    NOT NULL,
                day_type  INTEGER NOT NULL,
                mode      TEXT    DEFAULT 'normal',
                completed INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS sets (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id     INTEGER NOT NULL,
                exercise_name  TEXT    NOT NULL,
                is_alternative INTEGER DEFAULT 0,
                set_number     INTEGER NOT NULL,
                weight         REAL,
                reps           INTEGER,
                FOREIGN KEY (workout_id) REFERENCES workouts(id)
            );
            CREATE TABLE IF NOT EXISTS body_weight (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                date       TEXT    NOT NULL,
                weight_kg  REAL    NOT NULL,
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS skips (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date    TEXT    NOT NULL,
                reason  TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS photos (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT    NOT NULL,
                file_id  TEXT    NOT NULL,
                date     TEXT    NOT NULL,
                caption  TEXT    DEFAULT ''
            );
        """)
        await conn.commit()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

async def get_config(key: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None


async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_user_by_tg_id(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM users") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def create_user(tg_id: int, username: str, name: str, cycle_day: int = 1) -> int:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, username, name, cycle_day, cycle_start_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_id, username, name, cycle_day, today),
        )
        await conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        # Уже существует — вернём id
        async with conn.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,)) as c:
            row = await c.fetchone()
            return row[0]


async def update_user_by_tg_id(tg_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [tg_id]
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(f"UPDATE users SET {fields} WHERE tg_id = ?", values)
        await conn.commit()


async def advance_cycle_by_tg_id(tg_id: int):
    """Продвигает цикл пользователя: 4→1, иначе +1"""
    user = await get_user_by_tg_id(tg_id)
    if not user:
        return
    new_day = 1 if user["cycle_day"] >= 4 else user["cycle_day"] + 1
    await update_user_by_tg_id(tg_id, cycle_day=new_day)


async def advance_cycle_by_db_id(db_id: int):
    """Продвигает цикл по внутреннему id (для ночного задания)"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT cycle_day FROM users WHERE id = ?", (db_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        new_day = 1 if row[0] >= 4 else row[0] + 1
        await conn.execute("UPDATE users SET cycle_day = ? WHERE id = ?", (new_day, db_id))
        await conn.commit()


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------

async def create_workout(user_id: int, workout_date: str, day_type: int, mode: str = "normal") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO workouts (user_id, date, day_type, mode) VALUES (?, ?, ?, ?)",
            (user_id, workout_date, day_type, mode),
        )
        await conn.commit()
        return cur.lastrowid


async def finish_workout(workout_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE workouts SET completed = 1 WHERE id = ?", (workout_id,))
        await conn.commit()


async def get_last_workout(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM workouts WHERE user_id = ? AND completed = 1 ORDER BY date DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Sets
# ---------------------------------------------------------------------------

async def add_set(
    workout_id: int,
    exercise_name: str,
    is_alternative: bool,
    set_number: int,
    weight: float,
    reps: int,
):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO sets (workout_id, exercise_name, is_alternative, set_number, weight, reps) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (workout_id, exercise_name, int(is_alternative), set_number, weight, reps),
        )
        await conn.commit()


async def get_max_weights(user_id: int) -> dict:
    """Максимальный вес по каждому упражнению за всё время"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT s.exercise_name, MAX(s.weight) AS max_weight
               FROM sets s
               JOIN workouts w ON s.workout_id = w.id
               WHERE w.user_id = ? AND s.weight IS NOT NULL
               GROUP BY s.exercise_name""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return {r["exercise_name"]: r["max_weight"] for r in rows}


async def get_first_workout_max_weights(user_id: int) -> dict:
    """Максимальный вес из первой завершённой тренировки"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id FROM workouts WHERE user_id = ? AND completed = 1 ORDER BY date ASC LIMIT 1",
            (user_id,),
        ) as cur:
            first = await cur.fetchone()
        if not first:
            return {}
        async with conn.execute(
            "SELECT exercise_name, MAX(weight) AS max_weight FROM sets WHERE workout_id = ? GROUP BY exercise_name",
            (first["id"],),
        ) as cur:
            rows = await cur.fetchall()
            return {r["exercise_name"]: r["max_weight"] for r in rows}


async def get_week_max_weights(user_id: int, week_start: str) -> dict:
    """Максимальные веса за неделю, начинающуюся с week_start (YYYY-MM-DD)"""
    week_end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT s.exercise_name, MAX(s.weight) AS max_weight
               FROM sets s
               JOIN workouts w ON s.workout_id = w.id
               WHERE w.user_id = ? AND w.date >= ? AND w.date <= ? AND s.weight IS NOT NULL
               GROUP BY s.exercise_name""",
            (user_id, week_start, week_end),
        ) as cur:
            rows = await cur.fetchall()
            return {r["exercise_name"]: r["max_weight"] for r in rows}


# ---------------------------------------------------------------------------
# Body weight
# ---------------------------------------------------------------------------

async def log_body_weight(user_id: int, weight_date: str, weight_kg: float):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO body_weight (user_id, date, weight_kg) VALUES (?, ?, ?)",
            (user_id, weight_date, weight_kg),
        )
        await conn.commit()


async def get_body_weight_recent(user_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM body_weight WHERE user_id = ? ORDER BY date DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_body_weight_this_week(user_id: int, week_start: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM body_weight WHERE user_id = ? AND date >= ? ORDER BY date DESC LIMIT 1",
            (user_id, week_start),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Skips
# ---------------------------------------------------------------------------

async def log_skip(user_id: int, skip_date: str, reason: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO skips (user_id, date, reason) VALUES (?, ?, ?)",
            (user_id, skip_date, reason),
        )
        await conn.commit()


async def get_skips_this_month(user_id: int) -> int:
    today = date.today()
    month_start = f"{today.year}-{today.month:02d}-01"
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM skips WHERE user_id = ? AND date >= ?",
            (user_id, month_start),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Streak
# ---------------------------------------------------------------------------

async def get_streak(user_id: int) -> int:
    """Серия — кол-во подряд идущих дней с тренировкой или пропуском"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT date FROM workouts WHERE user_id = ? AND completed = 1", (user_id,)
        ) as cur:
            workout_dates = {r["date"] for r in await cur.fetchall()}
        async with conn.execute(
            "SELECT date FROM skips WHERE user_id = ?", (user_id,)
        ) as cur:
            skip_dates = {r["date"] for r in await cur.fetchall()}

    all_dates = sorted(workout_dates | skip_dates, reverse=True)
    if not all_dates:
        return 0

    streak = 0
    check = date.today()
    for d_str in all_dates:
        d = date.fromisoformat(d_str)
        if d >= check - timedelta(days=1):
            streak += 1
            check = d
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Фото-галерея
# ---------------------------------------------------------------------------

async def save_photo(user_key: str, file_id: str, photo_date: str, caption: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO photos (user_key, file_id, date, caption) VALUES (?, ?, ?, ?)",
            (user_key, file_id, photo_date, caption or ""),
        )
        await conn.commit()
        return cur.lastrowid


async def get_photos_by_key(user_key: str) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM photos WHERE user_key = ? ORDER BY date DESC, id DESC",
            (user_key,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def move_photo_to_group(photo_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE photos SET user_key = 'group' WHERE id = ?", (photo_id,))
        await conn.commit()


async def get_photo_count_today(user_key: str, today: str) -> int:
    """Кількість фото для user_key за сьогодні."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM photos WHERE user_key = ? AND date = ?",
            (user_key, today),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------------------
# Прогрес за місяць і результати конкретного дня
# ---------------------------------------------------------------------------

async def get_month_progress(user_id: int, year_month: str) -> dict:
    """Перша і остання макс. вага по вправам за місяць (YYYY-MM)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT MIN(date) as first_d, MAX(date) as last_d FROM workouts "
            "WHERE user_id = ? AND completed = 1 AND strftime('%Y-%m', date) = ?",
            (user_id, year_month),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["first_d"]:
            return {}
        first_d, last_d = row["first_d"], row["last_d"]

        async def max_on_day(day: str) -> dict:
            async with conn.execute(
                "SELECT s.exercise_name, MAX(s.weight) as mw FROM sets s "
                "JOIN workouts w ON s.workout_id = w.id "
                "WHERE w.user_id = ? AND w.date = ? AND s.weight IS NOT NULL "
                "GROUP BY s.exercise_name",
                (user_id, day),
            ) as c2:
                return {r["exercise_name"]: r["mw"] for r in await c2.fetchall()}

        first_ws = await max_on_day(first_d)
        last_ws  = await max_on_day(last_d)
        all_ex   = sorted(set(first_ws) | set(last_ws))
        return {
            ex: {"start": first_ws.get(ex), "end": last_ws.get(ex),
                 "first_date": first_d, "last_date": last_d}
            for ex in all_ex
        }


async def get_body_weight_month(user_id: int, year_month: str) -> dict:
    """Перший і останній запис ваги тіла за місяць."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT weight_kg, date FROM body_weight "
            "WHERE user_id = ? AND strftime('%Y-%m', date) = ? ORDER BY date ASC LIMIT 1",
            (user_id, year_month),
        ) as cur:
            first = await cur.fetchone()
        async with conn.execute(
            "SELECT weight_kg, date FROM body_weight "
            "WHERE user_id = ? AND strftime('%Y-%m', date) = ? ORDER BY date DESC LIMIT 1",
            (user_id, year_month),
        ) as cur:
            last = await cur.fetchone()
    return {"start": dict(first) if first else None, "end": dict(last) if last else None}


async def get_last_workout_sets(user_id: int, day_type: int) -> dict:
    """Сети з останнього тренування цього типу. Повертає {exercise_name: [(weight, reps), ...]}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, date FROM workouts "
            "WHERE user_id = ? AND day_type = ? AND completed = 1 "
            "ORDER BY date DESC LIMIT 1",
            (user_id, day_type),
        ) as cur:
            wo = await cur.fetchone()
        if not wo:
            return {}
        async with conn.execute(
            "SELECT exercise_name, weight, reps FROM sets "
            "WHERE workout_id = ? ORDER BY exercise_name, set_number",
            (wo["id"],),
        ) as cur:
            rows = await cur.fetchall()
    result: dict = {}
    for r in rows:
        name = r["exercise_name"]
        if name not in result:
            result[name] = []
        if r["weight"] is not None:
            result[name].append((r["weight"], r["reps"]))
    return result


async def get_workouts_on_date(date_str: str) -> list:
    """Всі завершені тренування за дату з сетами."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT w.id, w.day_type, w.mode, u.name FROM workouts w "
            "JOIN users u ON w.user_id = u.id "
            "WHERE w.date = ? AND w.completed = 1",
            (date_str,),
        ) as cur:
            workouts = [dict(r) for r in await cur.fetchall()]
        for wo in workouts:
            async with conn.execute(
                "SELECT exercise_name, is_alternative, set_number, weight, reps "
                "FROM sets WHERE workout_id = ? ORDER BY exercise_name, set_number",
                (wo["id"],),
            ) as cur:
                wo["sets"] = [dict(r) for r in await cur.fetchall()]
    return workouts


async def get_skips_on_date(date_str: str) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT s.reason, u.name FROM skips s JOIN users u ON s.user_id = u.id "
            "WHERE s.date = ?",
            (date_str,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
