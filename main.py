"""
Телеграм-бот для учёта тренировок. Весь UI через инлайн-кнопки.
Текстовые команды оставлены только /start и /menu (точка входа).
"""

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# =============================================================================
# Константы
# =============================================================================

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

ADMIN_ID: int = 610835370
ADMIN_USERNAME: str = "luhghghg"

USERS_CONFIG: dict = {
    "lug":   {"name": "Луг",    "username": "luhghghg",        "tg_id": ADMIN_ID},
    "vova":  {"name": "Вова",   "username": "vovaterlikoskyu", "tg_id": None},
    "pozix": {"name": "Позикс", "username": "etoileshade8917", "tg_id": None},
}

ALLOWED_CHAT_ID: Optional[int] = None

# Відлік циклу: 1 червня 2026 (пн) = День 1
PLAN_START = date(2026, 6, 1)

# Тренування тільки починаючи з цієї дати
LAUNCH_DATE = date(2026, 6, 1)
LAUNCH_BLOCK_MSG = (
    "🎉 Добрий Ден!\n\n"
    "Сьогодні в Артьома ДР — тренування не буде 🥳\n"
    "Стартуємо з 1 червня 💪"
)

# =============================================================================
# Дані вправ
# =============================================================================

DAY_NAMES = {
    1: "Грудь + Трицепс",
    2: "Спина + Біцепс",
    3: "Ноги + Плечі",
    4: "Відпочинок",
}

UA_MONTHS = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня",  8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}

UA_MONTHS_NOM = {
    1: "Січень", 2: "Лютий",   3: "Березень", 4: "Квітень",
    5: "Травень", 6: "Червень", 7: "Липень",   8: "Серпень",
    9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
}

EXERCISES_BY_DAY: dict = {
    1: [
        {"name": "Жим гантелей на наклонній (верх)", "short": "Жим гант.",   "alts": []},
        {"name": "Хаммер",                            "short": "Хаммер",      "alts": ["Жим штанги"]},
        {"name": "Брусья",                            "short": "Брусья",      "alts": ["Кроссовер знизу вгору"]},
        {"name": "Тяга V-рукоятки",                   "short": "Тяга V",      "alts": []},
        {"name": "Французький жим",                   "short": "Фр. жим",    "alts": ["Жим вузьким хватом"]},
    ],
    2: [
        {"name": "Вертикальна тяга блоку",            "short": "Верт. тяга",  "alts": []},
        {"name": "Горизонтальна тяга блоку",          "short": "Гор. тяга",   "alts": []},
        {"name": "Тяга гантелей",                     "short": "Тяга гант.",  "alts": ["Т-бар", "Hammer row"]},
        {"name": "Лава Скотта",                       "short": "Лава Скотта", "alts": []},
        {"name": "Молотки",                           "short": "Молотки",     "alts": []},
        {"name": "Трапеції — шраги",                  "short": "Шраги",       "alts": []},
    ],
    3: [
        {"name": "Жим платформи",                                "short": "Жим платф.",   "alts": []},
        {"name": "Румунська тяга",                               "short": "Рум. тяга",    "alts": []},
        {"name": "Розгинання ніг",                               "short": "Розгин. ніг",  "alts": []},
        {"name": "Згинання ніг",                                 "short": "Згин. ніг",    "alts": []},
        {"name": "Ікри",                                         "short": "Ікри",         "alts": []},
        {"name": "Середня дельта — метелик з гантелями",         "short": "Сер. дельта",  "alts": []},
        {"name": "Передня дельта — підйом гантелі стоя",        "short": "Пер. дельта",  "alts": []},
        {"name": "Задня дельта — Горб",                         "short": "Задня дельта", "alts": ["Задня метелик"]},
    ],
}

# =============================================================================
# Стан сесій (у пам'яті)
# =============================================================================

active_sessions: dict = {}   # eff_tg_id → WorkoutSession
proxy_mode: dict = {}        # ADMIN_ID → {"target_tg_id", "target_name", "last_activity"}
awaiting_weight: dict = {}   # eff_tg_id → menu_message_id  (очікуємо введення ваги)


class ExerciseState:
    def __init__(self, cfg: dict):
        self.original_name: str = cfg["name"]
        self.name: str = cfg["name"]
        self.short: str = cfg["short"]
        self.alts: list = list(cfg["alts"])
        self.alt_index: int = -1
        self.status: str = "pending"
        self.sets: list = []
        self.prev_str: str = ""   # підходи з попереднього тренування

    def cycle_alt(self) -> bool:
        if not self.alts:
            return False
        # Цикл: оригінал(-1) → alt0 → alt1 → ... → оригінал(-1) → ...
        total = len(self.alts) + 1
        next_pos = (self.alt_index + 2) % total  # +2 бо alt_index починається з -1
        self.alt_index = next_pos - 1
        self.name = self.original_name if self.alt_index == -1 else self.alts[self.alt_index]
        return True

    def is_alt(self) -> bool:
        return self.alt_index >= 0

    def sets_str(self) -> str:
        if not self.sets:
            return "—"
        return " / ".join(f"{int(w) if w == int(w) else w}x{r}" for w, r in self.sets)

    def icon(self) -> str:
        return {"pending": "⬜", "active": "🔄", "done": "✅", "skipped": "❌"}.get(self.status, "⬜")


class WorkoutSession:
    def __init__(self, user_tg_id, user_name, cycle_day, chat_id, message_id, is_max=False):
        self.user_tg_id = user_tg_id
        self.user_name = user_name
        self.cycle_day = cycle_day
        self.chat_id = chat_id
        self.message_id = message_id
        self.is_max = is_max
        self.exercises: list[ExerciseState] = []
        self.current_idx: Optional[int] = None
        self.workout_id: Optional[int] = None
        self.started_at = datetime.now()
        self.last_activity = datetime.now()
        self.finished = False
        self.awaiting_confirm = False

    @property
    def active_ex(self) -> Optional[ExerciseState]:
        return self.exercises[self.current_idx] if self.current_idx is not None else None

    def touch(self):
        self.last_activity = datetime.now()

# =============================================================================
# Допоміжні функції
# =============================================================================

def fmt_date_ua(d: date) -> str:
    return f"{d.day} {UA_MONTHS[d.month]}"


def get_proxy_target_id(admin_id: int) -> Optional[int]:
    if admin_id not in proxy_mode:
        return None
    pm = proxy_mode[admin_id]
    if datetime.now() - pm["last_activity"] > timedelta(minutes=30):
        del proxy_mode[admin_id]
        return None
    return pm["target_tg_id"]


def effective_user_id(sender_id: int) -> int:
    if sender_id == ADMIN_ID:
        target = get_proxy_target_id(ADMIN_ID)
        if target:
            return target
    return sender_id


def cfg_by_tg_id(tg_id: int) -> Optional[dict]:
    for cfg in USERS_CONFIG.values():
        if cfg["tg_id"] == tg_id:
            return cfg
    return None


def cfg_key_by_tg_id(tg_id: int) -> Optional[str]:
    for key, cfg in USERS_CONFIG.items():
        if cfg["tg_id"] == tg_id:
            return key
    return None


def parse_sets(text: str) -> list:
    return [
        (float(w.replace(",", ".")), int(r))
        for w, r in re.findall(r"(\d+(?:[.,]\d+)?)\s*[xXхХ]\s*(\d+)", text)
    ]


def is_known_user(user) -> bool:
    """Тільки Луг, Вова і Позікс — решта ігнорується."""
    if not user:
        return False
    for cfg in USERS_CONFIG.values():
        if cfg["tg_id"] and cfg["tg_id"] == user.id:
            return True
        uname = cfg.get("username")
        if uname and user.username and uname.lower() == user.username.lower():
            return True
    return False


async def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if not is_known_user(user):
        return False
    chat_type = update.effective_chat.type
    uid = user.id
    if chat_type == "private" and uid == ADMIN_ID:
        return True
    if ALLOWED_CHAT_ID is None:
        return False
    return update.effective_chat.id == ALLOWED_CHAT_ID


async def ensure_db_user(tg_id: int) -> Optional[dict]:
    cfg = cfg_by_tg_id(tg_id)
    if not cfg:
        return None
    user = await db.get_user_by_tg_id(tg_id)
    if not user:
        uid = await db.create_user(tg_id, cfg.get("username") or "", cfg["name"])
        user = await db.get_user_by_id(uid)
    return user

# =============================================================================
# Побудова клавіатур
# =============================================================================

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="menu_main")]])


def plan_cycle_day(d: date) -> int:
    """День циклу (1-4) для довільної дати; 1 червня 2026 = День 1."""
    return (d - PLAN_START).days % 4 + 1


DAY_ICONS = {1: "💪", 2: "💪", 3: "💪", 4: "😴"}
UA_WEEKDAYS_LONG = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
UA_WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def build_plan_kb() -> InlineKeyboardMarkup:
    """Календар-клавіатура на червень 2026."""
    today = date.today()
    rows = []

    # Заголовок з днями тижня (некликабельні)
    rows.append([InlineKeyboardButton(wd, callback_data="noop") for wd in UA_WEEKDAYS_SHORT])

    # Червень 2026: 30 днів, 1-го — понеділок (weekday=0), зміщення = 0
    week: list = []
    for day_num in range(1, 31):
        d = date(2026, 6, day_num)
        cd = plan_cycle_day(d)
        icon = DAY_ICONS[cd]
        marker = "▶" if d == today else ""
        label = f"{marker}{icon}{day_num}"
        week.append(InlineKeyboardButton(label, callback_data=f"plan_{day_num}"))
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        rows.append(week)

    rows.append([InlineKeyboardButton("← Меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def plan_day_text_plan(day_num: int) -> str:
    """Плановий текст для майбутнього дня червня."""
    d = date(2026, 6, day_num)
    cd = plan_cycle_day(d)
    weekday = UA_WEEKDAYS_LONG[d.weekday()]
    lines = [f"📅 {day_num} червня — {weekday}", f"💪 День {cd} — {DAY_NAMES[cd]}", ""]
    if cd == 4:
        lines += ["😴 Відпочинок", "Цикл продовжується наступного дня."]
    else:
        lines.append("Заплановані вправи:")
        for ex in EXERCISES_BY_DAY[cd]:
            alts = f"  (alt: {', '.join(ex['alts'])})" if ex["alts"] else ""
            lines.append(f"  • {ex['name']}{alts}")
    return "\n".join(lines)


async def plan_day_text_results(day_num: int) -> str:
    """Текст з фактичними результатами для минулого дня червня."""
    d = date(2026, 6, day_num)
    date_str = d.isoformat()
    cd = plan_cycle_day(d)
    weekday = UA_WEEKDAYS_LONG[d.weekday()]

    lines = [f"📅 {day_num} червня — {weekday}", f"День {cd} — {DAY_NAMES[cd]}", ""]

    workouts = await db.get_workouts_on_date(date_str)
    skips    = await db.get_skips_on_date(date_str)

    trained_names = {wo["name"] for wo in workouts}
    skipped_names = {s["name"] for s in skips}
    all_names = {cfg["name"] for cfg in USERS_CONFIG.values() if cfg["tg_id"]}

    if not workouts and not skips:
        lines.append("— Ніхто не тренувався цього дня.")
        return "\n".join(lines)

    for wo in workouts:
        lines.append(f"🏋️ {wo['name']}:")
        # Групуємо сети по вправах
        sets_by_ex: dict = {}
        for s in wo["sets"]:
            sets_by_ex.setdefault(s["exercise_name"], []).append(s)
        if sets_by_ex:
            for ex_name, ex_sets in sets_by_ex.items():
                sets_str = " / ".join(
                    f"{int(s['weight']) if s['weight'] == int(s['weight']) else s['weight']}x{s['reps']}"
                    for s in ex_sets if s["weight"] is not None
                )
                lines.append(f"  ✅ {ex_name}: {sets_str or '—'}")
        else:
            lines.append("  (без даних)")
        lines.append("")

    for s in skips:
        lines.append(f"😴 {s['name']} — пропустив ({s['reason']})")

    not_trained = all_names - trained_names - skipped_names
    for name in not_trained:
        lines.append(f"⬜ {name} — не тренувався")

    return "\n".join(lines)


def plan_day_text_and_kb(day_num: int) -> tuple[str, InlineKeyboardMarkup]:
    """Синхронна обгортка для майбутніх днів (вертає план)."""
    txt = plan_day_text_plan(day_num)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("← Календар", callback_data="menu_plan"),
        InlineKeyboardButton("← Меню",     callback_data="menu_main"),
    ]])
    return txt, kb


# =============================================================================
# Клавіатури та тексти для Результатів
# =============================================================================

def build_results_user_kb() -> InlineKeyboardMarkup:
    btns = []
    for key, cfg in USERS_CONFIG.items():
        if cfg["tg_id"]:
            btns.append(InlineKeyboardButton(cfg["name"], callback_data=f"res_user_{key}"))
    return InlineKeyboardMarkup([btns, [InlineKeyboardButton("← Меню", callback_data="menu_main")]])


def build_results_month_kb(user_key: str) -> InlineKeyboardMarkup:
    today = date.today()
    rows, row = [], []
    for i in range(6):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        label = f"{UA_MONTHS_NOM[m]} {y}"
        row.append(InlineKeyboardButton(label, callback_data=f"res_m_{user_key}_{y}-{m:02d}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← Назад", callback_data="menu_results")])
    return InlineKeyboardMarkup(rows)


async def text_month_results(user_key: str, year_month: str) -> str:
    cfg = USERS_CONFIG.get(user_key)
    if not cfg or not cfg["tg_id"]:
        return "❌ Користувача не знайдено."
    db_user = await db.get_user_by_tg_id(cfg["tg_id"])
    if not db_user:
        return "❌ Немає даних."

    y, m = map(int, year_month.split("-"))
    month_label = f"{UA_MONTHS_NOM[m]} {y}"
    progress = await db.get_month_progress(db_user["id"], year_month)
    bw = await db.get_body_weight_month(db_user["id"], year_month)

    lines = [f"📊 {cfg['name']} — {month_label}", "━" * 34]
    if not progress:
        lines.append("Тренувань цього місяця не знайдено.")
    else:
        for ex, data in progress.items():
            s = data["start"]
            e = data["end"]
            if s is None and e is None:
                continue
            if s == e or s is None or e is None:
                lines.append(f"  {ex[:26]}: {e or s}кг")
            else:
                diff = e - s
                sign = "+" if diff > 0 else ""
                arrow = "🔼" if diff > 0 else ("🔽" if diff < 0 else "➡️")
                lines.append(f"  {ex[:26]}: {s}кг → {e}кг ({sign}{diff:.1f}) {arrow}")

    lines.append("━" * 34)
    if bw["start"] and bw["end"]:
        ws = bw["start"]["weight_kg"]
        we = bw["end"]["weight_kg"]
        dw = we - ws
        sign = "+" if dw > 0 else ""
        lines.append(f"⚖️ Вага: {ws}кг → {we}кг ({sign}{dw:.1f}кг)")
    else:
        lines.append("⚖️ Вага: даних немає")

    return "\n".join(lines)


# =============================================================================
# Клавіатури та логіка Галереї
# =============================================================================

GALLERY_KEYS = {
    "group": "📸 Спільні",
    "lug":   "👤 Луг",
    "vova":  "👤 Вова",
    "pozix": "👤 Позикс",
}


def can_access_gallery(sender_id: int, gallery_key: str) -> bool:
    """Чи може sender_id дивитися цю галерею."""
    if gallery_key == "group":
        return True
    if sender_id == ADMIN_ID:
        return True
    return cfg_key_by_tg_id(sender_id) == gallery_key


def build_gallery_cat_kb(sender_id: int) -> InlineKeyboardMarkup:
    """Показує тільки доступні для sender_id категорії."""
    rows = []
    row = []
    for key, label in GALLERY_KEYS.items():
        if can_access_gallery(sender_id, key):
            row.append(InlineKeyboardButton(label, callback_data=f"gal_cat_{key}"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def build_gallery_nav_kb(
    user_key: str, idx: int, total: int, photo_id: int, is_owner: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("← Попер.", callback_data=f"gal_nav_{user_key}_{idx - 1}"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("Наступ. →", callback_data=f"gal_nav_{user_key}_{idx + 1}"))
    if nav:
        rows.append(nav)

    # Кнопки редагування тільки власнику або адміну
    if is_owner:
        extra = []
        if user_key != "group":
            extra.append(InlineKeyboardButton("🌐 До спільних", callback_data=f"gal_tog_{photo_id}"))
        extra.append(InlineKeyboardButton("🗑 Видалити", callback_data=f"gal_del_{photo_id}_{user_key}"))
        rows.append(extra)

    rows.append([InlineKeyboardButton("✕ Закрити", callback_data="gal_close")])
    return InlineKeyboardMarkup(rows)


def gallery_caption(photo: dict, idx: int, total: int) -> str:
    d = photo.get("date", "")
    cap = photo.get("caption", "")
    key = photo.get("user_key", "")
    name = GALLERY_KEYS.get(key, key)
    parts = [f"{name} | {d}"]
    if cap:
        parts.append(cap)
    parts.append(f"({idx + 1}/{total})")
    return "\n".join(parts)


def build_main_menu_kb(sender_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("💪 Тренування",  callback_data="menu_workout"),
            InlineKeyboardButton("💥 Макс",         callback_data="menu_max"),
        ],
        [
            InlineKeyboardButton("😴 Пропустити",   callback_data="menu_skip"),
            InlineKeyboardButton("⚖️ Вага",          callback_data="menu_weight"),
        ],
        [
            InlineKeyboardButton("📊 Мій профіль",  callback_data="menu_me"),
            InlineKeyboardButton("🏆 Статус групи", callback_data="menu_status"),
        ],
        [
            InlineKeyboardButton("⚔️ Порівняння",   callback_data="menu_vs"),
            InlineKeyboardButton("📅 План",          callback_data="menu_plan"),
        ],
        [
            InlineKeyboardButton("📊 Результати",   callback_data="menu_results"),
            InlineKeyboardButton("📸 Галерея",       callback_data="menu_gallery"),
        ],
    ]
    if sender_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("👤 Прокси-режим", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def build_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤒 Хворий",      callback_data="skip_sick"),
            InlineKeyboardButton("😴 Відпочинок",  callback_data="skip_rest"),
        ],
        [
            InlineKeyboardButton("✈️ Поїздка",     callback_data="skip_travel"),
            InlineKeyboardButton("🔧 Інша",        callback_data="skip_other"),
        ],
        [InlineKeyboardButton("← Меню", callback_data="menu_main")],
    ])


def build_vs_kb(sender_id: int) -> InlineKeyboardMarkup:
    rows = []
    btns = []
    for key, cfg in USERS_CONFIG.items():
        if cfg["tg_id"] and cfg["tg_id"] != sender_id:
            btns.append(InlineKeyboardButton(cfg["name"], callback_data=f"vs_{key}"))
    if btns:
        rows.append(btns)
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def build_admin_kb() -> InlineKeyboardMarkup:
    btns = []
    for key, cfg in USERS_CONFIG.items():
        active = ADMIN_ID in proxy_mode and proxy_mode[ADMIN_ID]["target_tg_id"] == cfg["tg_id"]
        label = f"✅ {cfg['name']}" if (key == "lug" and ADMIN_ID not in proxy_mode) or active else cfg["name"]
        btns.append(InlineKeyboardButton(label, callback_data=f"proxy_{key}"))
    return InlineKeyboardMarkup([btns, [InlineKeyboardButton("← Меню", callback_data="menu_main")]])


def build_workout_text(session: WorkoutSession, proxy_for: str = None) -> str:
    today = date.today()
    header = f"💪 {session.user_name} — День {session.cycle_day} ({DAY_NAMES[session.cycle_day]}) | {fmt_date_ua(today)}"
    if session.is_max:
        header += " | 💥 МАКС"
    lines = []
    if proxy_for:
        lines += [f"📝 Записуєш за {proxy_for}", ""]
    lines += [header, "━" * 38]
    for ex in session.exercises:
        sets_part = ex.sets_str()
        if ex.status == "active":
            sets_part += " ← вводиш зараз"
        lines.append(f"{ex.icon()} {ex.name[:36]:<36} {sets_part}")
        # Показуємо попередні підходи тільки для ще не зроблених вправ
        if ex.prev_str and ex.status in ("pending", "active"):
            lines.append(f"   ↳ {ex.prev_str}")
    lines.append("━" * 38)
    return "\n".join(lines)


def build_workout_kb(session: WorkoutSession) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, ex in enumerate(session.exercises):
        row.append(InlineKeyboardButton(f"{ex.short} {ex.icon()}", callback_data=f"ex_sel_{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔁 Альт.",      callback_data="ex_alt"),
        InlineKeyboardButton("😴 Пропустити", callback_data="ex_skip"),
        InlineKeyboardButton("🏁 Готово",     callback_data="workout_done"),
    ])
    return InlineKeyboardMarkup(rows)


def build_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Так ✅", callback_data="confirm_yes"),
        InlineKeyboardButton("Ні ❌",  callback_data="confirm_no"),
    ]])

# =============================================================================
# Побудова текстів (для кнопок статистики)
# =============================================================================

async def text_main_menu(sender_id: int, eff_id: int) -> str:
    cfg = cfg_by_tg_id(eff_id)
    name = cfg["name"] if cfg else "???"
    db_user = await db.get_user_by_tg_id(eff_id) if cfg else None
    cycle_line = ""
    if db_user:
        d = db_user["cycle_day"]
        cycle_line = f"\n{name} | День {d} — {DAY_NAMES[d]}"
    proxy_line = ""
    if sender_id == ADMIN_ID and ADMIN_ID in proxy_mode:
        proxy_line = f"\n📝 Режим прокси: за {proxy_mode[ADMIN_ID]['target_name']}"
    return f"🏋️ Головне меню{cycle_line}{proxy_line}"


async def text_me(eff_id: int) -> str:
    cfg = cfg_by_tg_id(eff_id)
    if not cfg:
        return "❌ Ви не зареєстровані."
    db_user = await db.get_user_by_tg_id(eff_id)
    if not db_user:
        return "❌ Ще не починав тренуватися."

    day = db_user["cycle_day"]
    last_wo = await db.get_last_workout(db_user["id"])
    streak = await db.get_streak(db_user["id"])
    skips_m = await db.get_skips_this_month(db_user["id"])
    weights = await db.get_body_weight_recent(db_user["id"], 10)

    last_str = "—"
    if last_wo:
        diff = (date.today() - date.fromisoformat(last_wo["date"])).days
        last_str = "сьогодні" if diff == 0 else ("вчора" if diff == 1 else f"{diff}д тому")

    w_str, w_change = "—", ""
    if weights:
        cur_w = weights[0]["weight_kg"]
        w_str = f"{cur_w} кг"
        if len(weights) >= 2:
            dw = cur_w - weights[-1]["weight_kg"]
            w_change = f" ({'+' if dw >= 0 else ''}{dw:.1f} кг)"

    max_ws = await db.get_max_weights(db_user["id"])
    first_ws = await db.get_first_workout_max_weights(db_user["id"])

    lines = [
        f"📊 {cfg['name']} — Особиста картка",
        "━" * 32,
        f"🔄 День циклу: День {day} ({DAY_NAMES[day]})",
        f"📅 Останнє тренування: {last_str}",
        f"🔥 Серія: {streak} тренувань",
        f"😴 Пропуски цього місяця: {skips_m}",
        f"⚖️ Вага: {w_str}{w_change}",
        "━" * 32,
        "💪 Топ ваги:",
    ]
    for ex_name, max_w in list(max_ws.items())[:8]:
        first_w = first_ws.get(ex_name)
        prog = ""
        if first_w:
            dw = max_w - first_w
            prog = f"  ({'+' if dw >= 0 else ''}{dw:.1f} від першого)"
        lines.append(f"  {ex_name[:26]}: {max_w}кг{prog}")
    return "\n".join(lines)


async def text_status() -> str:
    lines = ["🏋️ Статус групи", "━" * 32]
    for cfg in USERS_CONFIG.values():
        if not cfg["tg_id"]:
            lines.append(f"{cfg['name']}: не зареєстрований")
            continue
        db_user = await db.get_user_by_tg_id(cfg["tg_id"])
        if not db_user:
            lines.append(f"{cfg['name']}: не починав")
            continue
        d = db_user["cycle_day"]
        last = await db.get_last_workout(db_user["id"])
        streak = await db.get_streak(db_user["id"])
        last_str = "—"
        if last:
            diff = (date.today() - date.fromisoformat(last["date"])).days
            last_str = "сьогодні" if diff == 0 else ("вчора" if diff == 1 else f"{diff}д тому")
        lines.append(f"{cfg['name']}: День {d} | {last_str} | 🔥{streak}")
    return "\n".join(lines)


async def text_vs(sender_id: int, target_key: str) -> str:
    u1_cfg = cfg_by_tg_id(sender_id)
    u2_cfg = USERS_CONFIG.get(target_key)
    if not u1_cfg or not u2_cfg or not u2_cfg["tg_id"]:
        return "❌ Користувача не знайдено."
    db1 = await db.get_user_by_tg_id(u1_cfg["tg_id"])
    db2 = await db.get_user_by_tg_id(u2_cfg["tg_id"])
    if not db1 or not db2:
        return "❌ Один з користувачів ще не тренувався."
    m1 = await db.get_max_weights(db1["id"])
    m2 = await db.get_max_weights(db2["id"])
    all_ex = sorted(set(m1) | set(m2))
    lines = [f"⚔️ {u1_cfg['name']} vs {u2_cfg['name']}", "━" * 36]
    for ex in all_ex:
        w1 = m1.get(ex, 0) or 0
        w2 = m2.get(ex, 0) or 0
        badge = (f"🏆 {u1_cfg['name']}" if w1 > w2
                 else (f"🏆 {u2_cfg['name']}" if w2 > w1 else "🤝"))
        lines.append(f"{ex[:22]:<22} {w1}кг vs {w2}кг  {badge}")
    return "\n".join(lines)

# =============================================================================
# Дії (спільні для команд і кнопок)
# =============================================================================

async def do_skip(eff_id: int, reason: str):
    db_user = await ensure_db_user(eff_id)
    if not db_user:
        return
    await db.log_skip(db_user["id"], date.today().isoformat(), reason)
    await db.advance_cycle_by_tg_id(eff_id)


async def start_workout_for_user(
    bot,
    eff_id: int,
    sender_id: int,
    chat_id: int,
    is_max: bool,
) -> tuple[bool, str]:
    if date.today() < LAUNCH_DATE:
        return False, LAUNCH_BLOCK_MSG

    db_user = await ensure_db_user(eff_id)
    if not db_user:
        return False, "❌ Ви не зареєстровані в системі."

    cycle_day = db_user["cycle_day"]
    cfg = cfg_by_tg_id(eff_id)
    user_name = cfg["name"] if cfg else "???"

    if cycle_day == 4:
        return False, f"😴 {user_name} — день відпочинку, цикл завтра продовжується"

    if eff_id in active_sessions and not active_sessions[eff_id].finished:
        return False, "❗ Тренування вже активне! Натисни 🏁 Готово щоб завершити."

    proxy_for: Optional[str] = None
    if sender_id == ADMIN_ID and eff_id != ADMIN_ID and ADMIN_ID in proxy_mode:
        proxy_for = proxy_mode[ADMIN_ID]["target_name"]

    exercises = [ExerciseState(e) for e in EXERCISES_BY_DAY[cycle_day]]

    # Завантажуємо підходи з попереднього тренування цього типу
    prev_data = await db.get_last_workout_sets(db_user["id"], cycle_day)
    for ex in exercises:
        # Шукаємо за основною назвою або будь-якою альтернативою
        candidates = [ex.original_name] + ex.alts
        for candidate in candidates:
            if candidate in prev_data and prev_data[candidate]:
                sets = prev_data[candidate]
                sets_str = " / ".join(
                    f"{int(w) if w == int(w) else w}x{r}" for w, r in sets
                )
                ex.prev_str = sets_str
                break

    workout_id = await db.create_workout(db_user["id"], date.today().isoformat(), cycle_day,
                                         "max" if is_max else "normal")
    session = WorkoutSession(eff_id, user_name, cycle_day, chat_id, 0, is_max)
    session.exercises = exercises
    session.workout_id = workout_id

    msg = await bot.send_message(chat_id, build_workout_text(session, proxy_for),
                                 reply_markup=build_workout_kb(session))
    session.message_id = msg.message_id
    active_sessions[eff_id] = session

    if sender_id == ADMIN_ID and ADMIN_ID in proxy_mode:
        proxy_mode[ADMIN_ID]["last_activity"] = datetime.now()
    return True, ""


async def do_finish_session(
    ctx: ContextTypes.DEFAULT_TYPE,
    session: WorkoutSession,
    eff_id: int,
    force: bool,
    query=None,
):
    pending = [e for e in session.exercises if e.status == "pending"]
    if pending and not force:
        session.awaiting_confirm = True
        names = ", ".join(e.name for e in pending)
        txt = build_workout_text(session) + f"\n\n⚠️ Не виконано: {names}. Завершити?"
        if query:
            try:
                await query.edit_message_text(txt, reply_markup=build_confirm_kb())
            except Exception:
                pass
        return

    for ex in session.exercises:
        for i, (weight, reps) in enumerate(ex.sets, 1):
            await db.add_set(session.workout_id, ex.name, ex.is_alt(), i, weight, reps)
    await db.finish_workout(session.workout_id)
    if not session.is_max:
        await db.advance_cycle_by_tg_id(eff_id)
    session.finished = True

    done_count = sum(1 for e in session.exercises if e.status == "done")
    skipped = [e.name for e in session.exercises if e.status == "skipped"]
    lines = [f"🏁 {session.user_name} завершив тренування!", f"✅ Виконано: {done_count} вправ"]
    if skipped:
        lines.append(f"❌ Пропущено: {', '.join(skipped)}")

    if query:
        try:
            await query.edit_message_text("\n".join(lines), reply_markup=back_kb())
        except Exception:
            pass
    else:
        try:
            await ctx.bot.edit_message_text("\n".join(lines),
                                            chat_id=session.chat_id,
                                            message_id=session.message_id,
                                            reply_markup=back_kb())
        except Exception:
            pass

    # Нагадування про вагу тіла
    db_user = await db.get_user_by_tg_id(eff_id)
    if db_user:
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        if not await db.get_body_weight_this_week(db_user["id"], week_start):
            await ctx.bot.send_message(
                session.chat_id,
                f"⚖️ {session.user_name}, запиши вагу тіла цього тижня!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚖️ Записати вагу", callback_data="menu_weight"),
                    InlineKeyboardButton("← Меню",           callback_data="menu_main"),
                ]]),
            )

    if ADMIN_ID in proxy_mode and proxy_mode[ADMIN_ID]["target_tg_id"] == eff_id:
        proxy_mode.pop(ADMIN_ID, None)

# =============================================================================
# Команди (тільки /start і /menu як точки входу)
# =============================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_CHAT_ID
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup") and user.id == ADMIN_ID:
        if ALLOWED_CHAT_ID is None:
            ALLOWED_CHAT_ID = chat.id
            await db.set_config("allowed_chat_id", str(chat.id))
            await update.message.reply_text(f"✅ Чат зареєстровано! ID: {chat.id}")
        else:
            await update.message.reply_text(f"Чат вже зареєстровано (ID: {ALLOWED_CHAT_ID})")
        return

    if not await is_allowed(update):
        return
    await _send_menu(update.effective_user.id, update.effective_chat.id, ctx)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await _send_menu(update.effective_user.id, update.effective_chat.id, ctx)


async def _send_menu(sender_id: int, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    eff_id = effective_user_id(sender_id)
    txt = await text_main_menu(sender_id, eff_id)
    await ctx.bot.send_message(chat_id, txt, reply_markup=build_main_menu_kb(sender_id))


# /adduser — тільки текстом, бо потребує аргументів
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Використання: /adduser vova 123456789")
        return
    key = args[0].lower()
    if key not in ("vova", "pozix"):
        await update.message.reply_text("❌ Ключ: vova або pozix")
        return
    try:
        tg_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id")
        return
    USERS_CONFIG[key]["tg_id"] = tg_id
    name = USERS_CONFIG[key]["name"]
    await db.create_user(tg_id, USERS_CONFIG[key].get("username") or "", name)
    await update.message.reply_text(
        f"✅ {name} зареєстрований (ID: {tg_id})",
        reply_markup=back_kb(),
    )

# =============================================================================
# Головний callback handler
# =============================================================================

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_known_user(query.from_user):
        await query.answer()
        return
    await query.answer()

    sender_id = query.from_user.id
    eff_id = effective_user_id(sender_id)
    data = query.data
    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    # ── Головне меню ──────────────────────────────────────────────────────────
    if data == "menu_main":
        txt = await text_main_menu(sender_id, eff_id)
        try:
            await query.edit_message_text(txt, reply_markup=build_main_menu_kb(sender_id))
        except Exception:
            pass
        return

    # ── Тренування ────────────────────────────────────────────────────────────
    if data in ("menu_workout", "menu_max"):
        is_max = data == "menu_max"
        ok, err = await start_workout_for_user(ctx.bot, eff_id, sender_id, chat_id, is_max)
        if not ok:
            try:
                await query.edit_message_text(err, reply_markup=back_kb())
            except Exception:
                pass
        return

    # ── Пропустити ────────────────────────────────────────────────────────────
    if data == "menu_skip":
        try:
            await query.edit_message_text("😴 Причина пропуску:", reply_markup=build_skip_kb())
        except Exception:
            pass
        return

    if data.startswith("skip_"):
        reason_map = {
            "skip_sick":   "хворий",
            "skip_rest":   "відпочинок",
            "skip_travel": "поїздка",
            "skip_other":  "інша причина",
        }
        reason = reason_map.get(data, "—")
        await do_skip(eff_id, reason)
        cfg = cfg_by_tg_id(eff_id)
        name = cfg["name"] if cfg else "???"
        try:
            await query.edit_message_text(
                f"😴 {name} пропускає сьогодні ({reason}). Цикл просунуто.",
                reply_markup=back_kb(),
            )
        except Exception:
            pass
        return

    # ── Вага ──────────────────────────────────────────────────────────────────
    if data == "menu_weight":
        awaiting_weight[eff_id] = msg_id
        try:
            await query.edit_message_text(
                "⚖️ Введи вагу тіла у кілограмах\n(наприклад: 83.5)",
                reply_markup=back_kb(),
            )
        except Exception:
            pass
        return

    # ── Профіль ───────────────────────────────────────────────────────────────
    if data == "menu_me":
        txt = await text_me(eff_id)
        try:
            await query.edit_message_text(txt, reply_markup=back_kb())
        except Exception:
            pass
        return

    # ── Статус групи ──────────────────────────────────────────────────────────
    if data == "menu_status":
        txt = await text_status()
        try:
            await query.edit_message_text(txt, reply_markup=back_kb())
        except Exception:
            pass
        return

    # ── Порівняння ────────────────────────────────────────────────────────────
    if data == "menu_vs":
        try:
            await query.edit_message_text("⚔️ Оберіть суперника:", reply_markup=build_vs_kb(sender_id))
        except Exception:
            pass
        return

    if data.startswith("vs_"):
        target_key = data[3:]
        txt = await text_vs(sender_id, target_key)
        try:
            await query.edit_message_text(txt, reply_markup=back_kb())
        except Exception:
            pass
        return

    # ── Адмін / Прокси ────────────────────────────────────────────────────────
    if data == "menu_admin" and sender_id == ADMIN_ID:
        pm = proxy_mode.get(ADMIN_ID)
        cur = f"за {pm['target_name']}" if pm else "за себе (Луг)"
        try:
            await query.edit_message_text(
                f"👤 Прокси-режим\nЗараз пишеш: {cur}\n\nОберіть юзера:",
                reply_markup=build_admin_kb(),
            )
        except Exception:
            pass
        return

    # ── План (календар червня) ────────────────────────────────────────────────
    if data == "menu_plan":
        try:
            await query.edit_message_text(
                "📅 План тренувань — червень 2026\n💪 тренування  😴 відпочинок  ▶ сьогодні",
                reply_markup=build_plan_kb(),
            )
        except Exception:
            pass
        return

    if data.startswith("plan_"):
        try:
            day_num = int(data.removeprefix("plan_"))
            d = date(2026, 6, day_num)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("← Календар", callback_data="menu_plan"),
                InlineKeyboardButton("← Меню",     callback_data="menu_main"),
            ]])
            if d <= date.today():
                # Минулий або сьогоднішній день — показуємо реальні результати
                txt = await plan_day_text_results(day_num)
            else:
                txt = plan_day_text_plan(day_num)
            await query.edit_message_text(txt, reply_markup=kb)
        except Exception:
            pass
        return

    if data == "noop":
        return

    # ── Результати ────────────────────────────────────────────────────────────
    if data == "menu_results":
        try:
            await query.edit_message_text(
                "📊 Результати — оберіть гравця:",
                reply_markup=build_results_user_kb(),
            )
        except Exception:
            pass
        return

    if data.startswith("res_user_"):
        user_key = data.removeprefix("res_user_")
        cfg = USERS_CONFIG.get(user_key, {})
        name = cfg.get("name", user_key)
        try:
            await query.edit_message_text(
                f"📊 {name} — оберіть місяць:",
                reply_markup=build_results_month_kb(user_key),
            )
        except Exception:
            pass
        return

    if data.startswith("res_m_"):
        parts = data.removeprefix("res_m_").split("_", 1)
        if len(parts) == 2:
            u_key, year_month = parts
            txt = await text_month_results(u_key, year_month)
            try:
                await query.edit_message_text(
                    txt,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("← Місяці",  callback_data=f"res_user_{u_key}"),
                        InlineKeyboardButton("← Меню",    callback_data="menu_main"),
                    ]]),
                )
            except Exception:
                pass
        return

    # ── Галерея ───────────────────────────────────────────────────────────────
    if data == "menu_gallery":
        try:
            await query.edit_message_text(
                "📸 Галерея — оберіть категорію:",
                reply_markup=build_gallery_cat_kb(sender_id),
            )
        except Exception:
            pass
        return

    if data.startswith("gal_cat_"):
        user_key = data.removeprefix("gal_cat_")
        if not can_access_gallery(sender_id, user_key):
            try:
                await query.edit_message_text(
                    "❌ Немає доступу до цієї галереї.",
                    reply_markup=build_gallery_cat_kb(sender_id),
                )
            except Exception:
                pass
            return
        photos = await db.get_photos_by_key(user_key)
        if not photos:
            cat_label = GALLERY_KEYS.get(user_key, user_key)
            try:
                await query.edit_message_text(
                    f"📷 {cat_label} — фото ще немає.\n\n"
                    "Надішли фото в чат — збережеться автоматично (1 фото/день).",
                    reply_markup=build_gallery_cat_kb(sender_id),
                )
            except Exception:
                pass
            return
        p = photos[0]
        is_owner = (sender_id == ADMIN_ID) or (cfg_key_by_tg_id(sender_id) == user_key)
        kb = build_gallery_nav_kb(user_key, 0, len(photos), p["id"], is_owner)
        try:
            await query.message.reply_photo(
                photo=p["file_id"],
                caption=gallery_caption(p, 0, len(photos)),
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    if data.startswith("gal_nav_"):
        rest = data.removeprefix("gal_nav_")
        last_u = rest.rfind("_")
        user_key = rest[:last_u]
        idx = int(rest[last_u + 1:])
        if not can_access_gallery(sender_id, user_key):
            try:
                await ctx.bot.send_message(chat_id, "❌ Немає доступу до цієї галереї.")
            except Exception:
                pass
            return
        photos = await db.get_photos_by_key(user_key)
        if not photos or idx >= len(photos):
            try:
                await ctx.bot.send_message(chat_id, "⚠️ Фото не знайдено.")
            except Exception:
                pass
            return
        p = photos[idx]
        is_owner = (sender_id == ADMIN_ID) or (cfg_key_by_tg_id(sender_id) == user_key)
        kb = build_gallery_nav_kb(user_key, idx, len(photos), p["id"], is_owner)
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(media=p["file_id"], caption=gallery_caption(p, idx, len(photos))),
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    if data.startswith("gal_tog_"):
        photo_id = int(data.removeprefix("gal_tog_"))
        sender_key = cfg_key_by_tg_id(sender_id)
        photo_key = None
        async with __import__("aiosqlite").connect(db.DB_PATH) as _c:
            _c.row_factory = __import__("aiosqlite").Row
            async with _c.execute("SELECT user_key FROM photos WHERE id = ?", (photo_id,)) as _cur:
                _row = await _cur.fetchone()
                if _row:
                    photo_key = _row["user_key"]
        if sender_id != ADMIN_ID and sender_key != photo_key:
            await ctx.bot.send_message(chat_id, "❌ Можна переміщати тільки своє фото.")
            return
        today_str = date.today().isoformat()
        if await db.get_photo_count_today("group", today_str) >= 1:
            await ctx.bot.send_message(chat_id, "❌ Ліміт: 1 фото в спільні на день вже вичерпано.")
            return
        await db.move_photo_to_group(photo_id)
        await ctx.bot.send_message(chat_id, "✅ Фото переміщено до спільних!")
        return

    if data.startswith("gal_del_"):
        parts = data.removeprefix("gal_del_").split("_", 1)
        if len(parts) == 2:
            photo_id = int(parts[0])
            del_key = parts[1]
            sender_key = cfg_key_by_tg_id(sender_id)
            if sender_id != ADMIN_ID and sender_key != del_key:
                await ctx.bot.send_message(chat_id, "❌ Можна видаляти тільки своє фото.")
                return
            async with __import__("aiosqlite").connect(db.DB_PATH) as conn:
                await conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
                await conn.commit()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "gal_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("proxy_") and sender_id == ADMIN_ID:
        key = data[6:]
        if key == "lug":
            proxy_mode.pop(ADMIN_ID, None)
            txt = "✅ Пишеш за себе (Луг)"
        else:
            cfg = USERS_CONFIG.get(key, {})
            if not cfg.get("tg_id"):
                await query.answer(f"❌ {cfg.get('name','?')} не зареєстрований. /adduser {key} <id>",
                                   show_alert=True)
                return
            proxy_mode[ADMIN_ID] = {
                "target_tg_id": cfg["tg_id"],
                "target_name": {"vova": "Вову", "pozix": "Позикса"}.get(key, cfg["name"]),
                "last_activity": datetime.now(),
            }
            txt = f"✅ Режим прокси: за {proxy_mode[ADMIN_ID]['target_name']}"
        try:
            await query.edit_message_text(txt, reply_markup=back_kb())
        except Exception:
            pass
        return

    # ── Кнопки тренування ─────────────────────────────────────────────────────
    session = active_sessions.get(eff_id)
    # Дозволяємо власнику сесії натискати кнопки напряму (не через прокси)
    if not session and eff_id != sender_id:
        session = active_sessions.get(sender_id)
        if session:
            eff_id = sender_id

    if not session or session.finished:
        await query.answer("Сесія не активна", show_alert=True)
        return
    if query.message.message_id != session.message_id:
        return

    session.touch()

    if data.startswith("ex_sel_"):
        idx = int(data.removeprefix("ex_sel_"))
        if session.current_idx is not None:
            prev = session.exercises[session.current_idx]
            if prev.status == "active" and not prev.sets:
                prev.status = "pending"
        session.current_idx = idx
        session.exercises[idx].status = "active"

    elif data == "ex_alt":
        if session.current_idx is None:
            await query.answer("Спочатку вибери вправу", show_alert=True)
            return
        ex = session.exercises[session.current_idx]
        if not ex.alts:
            await query.answer("Немає альтернативи", show_alert=True)
            return
        ex.cycle_alt()
        ex.sets = []
        ex.status = "active"

    elif data == "ex_skip":
        if session.current_idx is None:
            await query.answer("Спочатку вибери вправу", show_alert=True)
            return
        session.exercises[session.current_idx].status = "skipped"
        session.current_idx = None

    elif data == "workout_done":
        if not session.awaiting_confirm:
            await do_finish_session(ctx, session, eff_id, force=False, query=query)
        return

    elif data == "confirm_yes":
        session.awaiting_confirm = False
        await do_finish_session(ctx, session, eff_id, force=True, query=query)
        return

    elif data == "confirm_no":
        session.awaiting_confirm = False

    # Оновлюємо повідомлення тренування
    proxy_for: Optional[str] = None
    if sender_id == ADMIN_ID and eff_id != ADMIN_ID and ADMIN_ID in proxy_mode:
        proxy_for = proxy_mode[ADMIN_ID]["target_name"]
    try:
        await query.edit_message_text(
            build_workout_text(session, proxy_for),
            reply_markup=build_workout_kb(session),
        )
    except Exception:
        pass

# =============================================================================
# Обробник текстових повідомлень (введення підходів і ваги тіла)
# =============================================================================

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    sender_id = update.effective_user.id
    eff_id = effective_user_id(sender_id)
    text = update.message.text.strip()

    # ── "Добрий Ден" → головне меню ──────────────────────────────────────────
    if text.lower() == "добрий ден":
        await _send_menu(sender_id, update.effective_chat.id, ctx)
        return

    # ── Очікуємо введення ваги тіла ──────────────────────────────────────────
    if eff_id in awaiting_weight:
        try:
            kg = float(text.replace(",", "."))
        except ValueError:
            return
        db_user = await ensure_db_user(eff_id)
        if db_user:
            await db.log_body_weight(db_user["id"], date.today().isoformat(), kg)
        cfg = cfg_by_tg_id(eff_id)
        name = cfg["name"] if cfg else "???"
        menu_msg_id = awaiting_weight.pop(eff_id)
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            await ctx.bot.edit_message_text(
                f"⚖️ {name}: {kg} кг записано!\n\n" + await text_main_menu(sender_id, eff_id),
                chat_id=update.effective_chat.id,
                message_id=menu_msg_id,
                reply_markup=build_main_menu_kb(sender_id),
            )
        except Exception:
            await update.message.reply_text(f"⚖️ {name}: {kg} кг записано!")
        return

    # ── Введення підходів для активного тренування ───────────────────────────
    session = active_sessions.get(eff_id)
    if not session or session.finished or session.current_idx is None:
        return

    sets = parse_sets(text)
    if not sets:
        return

    ex = session.exercises[session.current_idx]
    ex.sets = sets
    ex.status = "done"
    session.current_idx = None
    session.touch()

    try:
        await update.message.delete()
    except Exception:
        pass

    proxy_for: Optional[str] = None
    if sender_id == ADMIN_ID and eff_id != ADMIN_ID and ADMIN_ID in proxy_mode:
        proxy_for = proxy_mode[ADMIN_ID]["target_name"]
        proxy_mode[ADMIN_ID]["last_activity"] = datetime.now()

    try:
        await ctx.bot.edit_message_text(
            build_workout_text(session, proxy_for),
            chat_id=session.chat_id,
            message_id=session.message_id,
            reply_markup=build_workout_kb(session),
        )
    except Exception:
        pass

# =============================================================================
# Планувальник (APScheduler)
# =============================================================================

async def job_daily_plan(bot):
    """Щоранку о 08:00 — повідомлення що сьогодні за тренування."""
    if not ALLOWED_CHAT_ID:
        return
    today = date.today()
    cd = plan_cycle_day(today)
    weekday = UA_WEEKDAYS_LONG[today.weekday()]
    day_label = fmt_date_ua(today)

    if cd == 4:
        text = f"🔔 Пацани, {day_label} ({weekday})\n\n😴 Сьогодні — День відпочинку!\nВідновлюємось 💤"
    else:
        exs = "\n".join(f"  • {e['name']}" for e in EXERCISES_BY_DAY[cd])
        text = (
            f"🔔 Пацани, {day_label} ({weekday})\n\n"
            f"💪 Сьогодні — День {cd}: {DAY_NAMES[cd]}\n\n"
            f"{exs}"
        )
    await bot.send_message(ALLOWED_CHAT_ID, text)


async def job_weekly_summary(bot):
    if not ALLOWED_CHAT_ID:
        return
    today = date.today()
    week_num = today.isocalendar()[1]
    mon = today - timedelta(days=today.weekday())
    this_start = mon.isoformat()
    prev_start = (mon - timedelta(days=7)).isoformat()

    user_data: dict = {}
    for cfg in USERS_CONFIG.values():
        if not cfg["tg_id"]:
            continue
        db_user = await db.get_user_by_tg_id(cfg["tg_id"])
        if not db_user:
            continue
        user_data[cfg["name"]] = {
            "this":  await db.get_week_max_weights(db_user["id"], this_start),
            "prev":  await db.get_week_max_weights(db_user["id"], prev_start),
            "skips": await db.get_skips_this_month(db_user["id"]),
        }
    if not user_data:
        return

    names = list(user_data.keys())
    all_ex = sorted({ex for d in user_data.values() for ex in d["this"]})
    header = "               " + "   ".join(f"{n[:6]:<6}" for n in names)
    lines = [f"📊 Тиждень {week_num} — Підсумки", "━" * 36, header]
    for ex in all_ex:
        row = f"{ex[:14]:<14}"
        for n in names:
            w = user_data[n]["this"].get(ex)
            row += f"  {(str(w)+'кг') if w else '—':>7}"
        lines.append(row)

    lines += ["━" * 36, "📈 Прогрес за тиждень:"]
    for name, d in user_data.items():
        gains = []
        for ex, w in d["this"].items():
            pw = d["prev"].get(ex)
            if pw and w and w > pw:
                gains.append(f"{ex[:10]} +{w-pw:.1f}кг")
        lines.append(f"{name} — {', '.join(gains[:2]) + ' 🔥' if gains else 'без змін'}")

    skip_str = " | ".join(f"{n} {d['skips']}" for n, d in user_data.items())
    lines.append(f"😴 Пропуски: {skip_str}")
    await bot.send_message(ALLOWED_CHAT_ID, "\n".join(lines))


async def job_midnight_day4():
    for u in await db.get_all_users():
        if u["cycle_day"] == 4:
            await db.advance_cycle_by_db_id(u["id"])
            log.info("Auto-advanced Day4 for user %s", u["name"])

# =============================================================================
# Ініціалізація і запуск
# =============================================================================

async def post_init(application: Application):
    global ALLOWED_CHAT_ID
    await db.init_db()

    saved = await db.get_config("allowed_chat_id")
    if saved:
        ALLOWED_CHAT_ID = int(saved)
        log.info("Loaded ALLOWED_CHAT_ID=%s", ALLOWED_CHAT_ID)

    for cfg in USERS_CONFIG.values():
        if cfg["tg_id"]:
            await db.create_user(cfg["tg_id"], cfg.get("username") or "", cfg["name"])

    tz = pytz.timezone("Europe/Kyiv")
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        lambda: asyncio.create_task(job_daily_plan(application.bot)),
        "cron", hour=8, minute=0,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_weekly_summary(application.bot)),
        "cron", day_of_week="sun", hour=20, minute=0,
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_midnight_day4()),
        "cron", hour=0, minute=5,
    )
    scheduler.start()
    log.info("Scheduler started")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автозбереження фото з групи в галерею відправника. Ліміт 1/день."""
    context  # required by PTB handler signature
    if not await is_allowed(update):
        return
    sender_id = update.effective_user.id
    eff_id    = effective_user_id(sender_id)

    # Знаходимо user_key відправника (з урахуванням прокси)
    user_key = cfg_key_by_tg_id(eff_id)
    if not user_key:
        return

    today_str = date.today().isoformat()

    # Перевірка ліміту: 1 фото на день у особисту галерею
    count_today = await db.get_photo_count_today(user_key, today_str)
    if count_today >= 1:
        cfg = USERS_CONFIG.get(user_key, {})
        name = cfg.get("name", user_key)
        note = await update.message.reply_text(
            f"❌ {name}, ліміт: 1 фото на день у особисту галерею вже досягнуто."
        )
        # Видаляємо нотатку через 5 секунд щоб не смітити
        await asyncio.sleep(5)
        try:
            await note.delete()
            await update.message.delete()
        except Exception:
            pass
        return

    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    await db.save_photo(user_key, photo.file_id, today_str, caption)
    # Тихо зберігаємо — фото залишається в чаті


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("menu",    cmd_menu))
    app.add_handler(CommandHandler("adduser", cmd_adduser))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
