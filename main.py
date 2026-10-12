# main.py
# Telegram-бот для продажи видео
# pip install aiogram
# aiogram 3.x

import asyncio
import sqlite3
import time
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ==================== КОНФИГ ====================
BOT_TOKEN = "8647752778:AAEMZ_JWxTImF34lEfoTz7UWp3hUV7SLNmQ"
ADMIN_IDS = [8754872846]              # <-- свои Telegram ID
# ================================================


# ==================== БАЗА ====================
conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

def init_db():
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        diamonds INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0,
        last_daily REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        file_type TEXT,
        category INTEGER,
        caption TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS user_videos (
        user_id INTEGER,
        video_id INTEGER,
        PRIMARY KEY (user_id, video_id)
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        ptype TEXT,
        amount REAL,
        status TEXT DEFAULT 'pending',
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    ''')
    # дефолтные настройки
    defaults = {
        'card_number': '0000 0000 0000 0000',
        'card_name': 'Иван И.',
        'channel_id': '',        # @username или -100xxxxx
        'channel_url': '',       # ссылка для кнопки "Подписаться"
        'sub_enabled': '0',      # 0 / 1
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
    conn.commit()

init_db()


def get_setting(key, default=''):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = cur.fetchone()
    return r[0] if r else default


def set_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()


def ensure_user(uid, uname):
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (uid, uname))
    if uname:
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (uname, uid))
    conn.commit()


def is_banned(uid):
    cur.execute("SELECT banned FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    return bool(r and r[0])


def get_user(uid):
    cur.execute("SELECT user_id, username, diamonds, banned, last_daily FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()


def add_diamonds(uid, n):
    cur.execute("UPDATE users SET diamonds = diamonds + ? WHERE user_id=?", (n, uid))
    conn.commit()


def set_banned(uid, b):
    cur.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if b else 0, uid))
    conn.commit()


def get_unseen(uid, category, count):
    if category:
        cur.execute('''SELECT id, file_id, file_type, caption FROM videos
                       WHERE category=? AND id NOT IN
                       (SELECT video_id FROM user_videos WHERE user_id=?)
                       ORDER BY RANDOM() LIMIT ?''', (category, uid, count))
    else:
        cur.execute('''SELECT id, file_id, file_type, caption FROM videos
                       WHERE id NOT IN (SELECT video_id FROM user_videos WHERE user_id=?)
                       ORDER BY RANDOM() LIMIT ?''', (uid, count))
    return cur.fetchall()


def mark_seen(uid, vid):
    cur.execute("INSERT OR IGNORE INTO user_videos (user_id, video_id) VALUES (?,?)", (uid, vid))
    conn.commit()


# ==================== БОТ ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ==================== СОСТОЯНИЯ ====================
class PayStates(StatesGroup):
    wait_screenshot = State()


class AdmStates(StatesGroup):
    broadcast = State()
    find_user = State()
    add_video = State()
    give_diam = State()
    edit_card_number = State()
    edit_card_name = State()
    edit_channel_id = State()
    edit_channel_url = State()


# ==================== ПОДПИСКА ====================
async def check_sub(uid):
    """True если подписка ОК (или проверка выключена)."""
    if get_setting('sub_enabled', '0') != '1':
        return True
    channel = get_setting('channel_id', '').strip()
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=uid)
        return member.status in ('creator', 'administrator', 'member')
    except Exception as e:
        # если бот не админ канала или канал неверный — не блокируем
        print("sub check err:", e)
        return True


def sub_kb():
    url = get_setting('channel_url', '').strip()
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="📢 Подписаться", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.from_user.id not in ADMIN_IDS:
            if event.data != "check_sub" and not await check_sub(event.from_user.id):
                await event.answer("❌ Сначала подпишитесь на канал!", show_alert=True)
                try:
                    await event.message.answer(
                        "📢 Для использования бота подпишитесь на канал:",
                        reply_markup=sub_kb()
                    )
                except Exception:
                    pass
                return
        return await handler(event, data)


dp.callback_query.middleware(SubMiddleware())


# ==================== ХЕЛПЕРЫ ====================
async def send_media(chat_id, file_id, ftype, caption=''):
    try:
        if ftype == 'video':
            await bot.send_video(chat_id, file_id, caption=caption or None)
        else:
            await bot.send_document(chat_id, file_id, caption=caption or None)
    except Exception as e:
        print("send_media err:", e)


async def deliver(uid, category, count):
    vids = get_unseen(uid, category, count)
    if not vids and category:
        cur.execute('''DELETE FROM user_videos WHERE user_id=? AND video_id IN
                       (SELECT id FROM videos WHERE category=?)''', (uid, category))
        conn.commit()
        vids = get_unseen(uid, category, count)
    if not vids:
        return 0
    for vid_id, fid, ftype, cap in vids:
        await send_media(uid, fid, ftype, cap)
        mark_seen(uid, vid_id)
    return len(vids)


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Купить видео — 2💎", callback_data="buy_video")],
        [InlineKeyboardButton(text="📦 Категория 1 — Мощное", callback_data="cat_1")],
        [InlineKeyboardButton(text="📦 Категория 2 — Любимое", callback_data="cat_2")],
        [InlineKeyboardButton(text="🎁 Категория 3 — Бесплатно (24ч)", callback_data="cat_3")],
        [InlineKeyboardButton(text="💎 Купить алмазы", callback_data="buy_diamonds")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
    ])


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔍 Поиск юзера", callback_data="adm_find")],
        [InlineKeyboardButton(text="➕ Добавить видео", callback_data="adm_addvideo")],
        [InlineKeyboardButton(text="💳 Реквизиты карты", callback_data="adm_card")],
        [InlineKeyboardButton(text="📡 Канал подписки", callback_data="adm_channel")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
    ])


# ==================== ЮЗЕР ====================
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id, msg.from_user.username)
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы забанены.")

    if msg.from_user.id not in ADMIN_IDS and not await check_sub(msg.from_user.id):
        return await msg.answer(
            "📢 Для использования бота подпишитесь на канал:",
            reply_markup=sub_kb()
        )

    await msg.answer("👋 Добро пожаловать!\n\nВыбери действие:", reply_markup=main_kb())


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    if await check_sub(cb.from_user.id):
        try:
            await cb.message.edit_text("👋 Добро пожаловать!\n\nВыбери действие:", reply_markup=main_kb())
        except Exception:
            await cb.message.answer("👋 Добро пожаловать!", reply_markup=main_kb())
        await cb.answer("✅ Спасибо за подписку!")
    else:
        await cb.answer("❌ Вы ещё не подписались!", show_alert=True)


@router.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("👋 Главное меню:", reply_markup=main_kb())
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    if not u:
        return await cb.answer("Нет данных", show_alert=True)
    text = f"👤 <b>Ваш профиль</b>\n\nID: <code>{u[0]}</code>\n💎 Алмазы: <b>{u[2]}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "buy_video")
async def buy_video(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        return await cb.answer("Забанен", show_alert=True)
    u = get_user(cb.from_user.id)
    if u[2] < 2:
        return await cb.answer("❌ Недостаточно алмазов (нужно 2💎)", show_alert=True)
    add_diamonds(cb.from_user.id, -2)
    n = await deliver(cb.from_user.id, None, 1)
    if n == 0:
        add_diamonds(cb.from_user.id, 2)
        return await cb.answer("В базе нет видео", show_alert=True)
    await cb.answer("✅ Отправлено! Списано 2💎", show_alert=True)


@router.callback_query(F.data == "cat_3")
async def cat3(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        return await cb.answer("Забанен", show_alert=True)
    u = get_user(cb.from_user.id)
    now = time.time()
    if now - u[4] < 86400:
        left = int(86400 - (now - u[4]))
        h = left // 3600
        m = (left % 3600) // 60
        return await cb.answer(f"⏳ Следующий пак через {h}ч {m}м", show_alert=True)
    cur.execute("UPDATE users SET last_daily=? WHERE user_id=?", (now, cb.from_user.id))
    conn.commit()
    n = await deliver(cb.from_user.id, 3, 2)
    await cb.answer(f"🎁 Отправлено {n} видео!", show_alert=True)


@router.callback_query(F.data == "cat_1")
async def cat1(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        return await cb.answer("Забанен", show_alert=True)
    card = get_setting('card_number')
    cname = get_setting('card_name')
    text = (f"📦 <b>Категория 1 — Мощное</b>\n\n"
            f"🎬 10 видео\n💰 Цена: <b>120₽</b> (или 100⭐)\n\n"
            f"💳 Переведите <b>120₽</b> на карту:\n"
            f"<code>{card}</code>\n{cname}\n\n"
            f"После оплаты нажмите «Я оплатил» и отправьте скриншот/чек.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="confirm|cat1|120")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "cat_2")
async def cat2(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        return await cb.answer("Забанен", show_alert=True)
    card = get_setting('card_number')
    cname = get_setting('card_name')
    text = (f"📦 <b>Категория 2 — Любимое</b>\n\n"
            f"🎬 15 видео\n💰 Цена: <b>170₽</b> (или 150⭐)\n\n"
            f"💳 Переведите <b>170₽</b> на карту:\n"
            f"<code>{card}</code>\n{cname}\n\n"
            f"После оплаты нажмите «Я оплатил» и отправьте скриншот/чек.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="confirm|cat2|170")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "buy_diamonds")
async def buy_diamonds(cb: CallbackQuery):
    if is_banned(cb.from_user.id):
        return await cb.answer("Забанен", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 💎 — 10₽", callback_data="diam|10")],
        [InlineKeyboardButton(text="25 💎 — 25₽", callback_data="diam|25")],
        [InlineKeyboardButton(text="50 💎 — 50₽", callback_data="diam|50")],
        [InlineKeyboardButton(text="100 💎 — 100₽", callback_data="diam|100")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])
    await cb.message.edit_text(
        "💎 <b>Покупка алмазов</b>\n\n1 алмаз = 1₽\nВыберите количество (от 10 до 100):",
        reply_markup=kb
    )
    await cb.answer()


@router.callback_query(F.data.startswith("diam|"))
async def diam_choice(cb: CallbackQuery):
    amount = int(cb.data.split("|")[1])
    card = get_setting('card_number')
    cname = get_setting('card_name')
    text = (f"💎 <b>{amount} алмазов</b>\n\n"
            f"💳 Переведите <b>{amount}₽</b> на карту:\n"
            f"<code>{card}</code>\n{cname}\n\n"
            f"После оплаты нажмите «Я оплатил» и отправьте скриншот/чек.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm|diam_{amount}|{amount}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="buy_diamonds")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("confirm|"))
async def confirm(cb: CallbackQuery, state: FSMContext):
    _, ptype, amount = cb.data.split("|")
    await state.update_data(ptype=ptype, amount=float(amount))
    await state.set_state(PayStates.wait_screenshot)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="back_main")]
    ])
    await cb.message.edit_text(
        "📸 Отправьте скриншот или файл чека об оплате.",
        reply_markup=kb
    )
    await cb.answer()


@router.message(PayStates.wait_screenshot)
async def got_screenshot(msg: Message, state: FSMContext):
    data = await state.get_data()
    ptype = data.get('ptype')
    amount = data.get('amount')

    if not (msg.photo or msg.document):
        return await msg.answer("❗ Отправьте фото или документ.")

    cur.execute(
        "INSERT INTO payments (user_id, ptype, amount, status, created_at) VALUES (?,?,?,?,?)",
        (msg.from_user.id, ptype, amount, 'pending', time.time())
    )
    pid = cur.lastrowid
    conn.commit()

    u = get_user(msg.from_user.id)
    uname = u[1] or '—'
    caption = (f"💰 <b>Новая заявка на оплату</b> #{pid}\n\n"
               f"👤 Юзер: <code>{msg.from_user.id}</code> (@{uname})\n"
               f"📦 Тип: <b>{ptype}</b>\n"
               f"💵 Сумма: <b>{amount}₽</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выдать", callback_data=f"pay_ok|{pid}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"pay_no|{pid}")],
    ])

    for adm in ADMIN_IDS:
        try:
            if msg.photo:
                await bot.send_photo(adm, msg.photo[-1].file_id, caption=caption, reply_markup=kb)
            else:
                await bot.send_document(adm, msg.document.file_id, caption=caption, reply_markup=kb)
        except Exception as e:
            print("adm notify err:", e)

    await state.clear()
    await msg.answer("✅ Заявка отправлена админу. Ожидайте подтверждения.")


@router.callback_query(F.data.startswith("pay_ok|"))
async def pay_ok(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    pid = int(cb.data.split("|")[1])
    cur.execute("SELECT user_id, ptype, amount, status FROM payments WHERE id=?", (pid,))
    row = cur.fetchone()
    if not row:
        return await cb.answer("Не найдено", show_alert=True)
    uid, ptype, amount, status = row
    if status != 'pending':
        return await cb.answer("Уже обработано", show_alert=True)

    cur.execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
    conn.commit()

    try:
        if ptype == 'cat1':
            n = await deliver(uid, 1, 10)
            await bot.send_message(uid, f"✅ Оплата подтверждена!\nОтправлено {n} видео из категории 1 🎬")
        elif ptype == 'cat2':
            n = await deliver(uid, 2, 15)
            await bot.send_message(uid, f"✅ Оплата подтверждена!\nОтправлено {n} видео из категории 2 🎬")
        elif ptype.startswith('diam_'):
            cnt = int(ptype.split('_')[1])
            add_diamonds(uid, cnt)
            await bot.send_message(uid, f"✅ Оплата подтверждена!\nНачислено {cnt} 💎")
    except Exception as e:
        print("deliver err:", e)

    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption=cb.message.caption + "\n\n✅ ВЫДАНО")
        else:
            await cb.message.edit_text(text=(cb.message.text or '') + "\n\n✅ ВЫДАНО")
    except Exception:
        pass
    await cb.answer("✅ Выдано")


@router.callback_query(F.data.startswith("pay_no|"))
async def pay_no(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    pid = int(cb.data.split("|")[1])
    cur.execute("SELECT user_id, status FROM payments WHERE id=?", (pid,))
    row = cur.fetchone()
    if not row:
        return await cb.answer("Не найдено", show_alert=True)
    uid, status = row
    if status != 'pending':
        return await cb.answer("Уже обработано", show_alert=True)

    cur.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
    conn.commit()
    try:
        await bot.send_message(uid, "❌ Ваша оплата была отклонена администратором.")
    except Exception:
        pass
    try:
        if cb.message.caption:
            await cb.message.edit_caption(caption=cb.message.caption + "\n\n❌ ОТКЛОНЕНО")
        else:
            await cb.message.edit_text(text=(cb.message.text or '') + "\n\n❌ ОТКЛОНЕНО")
    except Exception:
        pass
    await cb.answer("Отклонено")


# ==================== АДМИН-ПАНЕЛЬ ====================
@router.message(Command("admin"))
async def admin_cmd(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await msg.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "adm_back")
async def adm_back(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.clear()
    await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
    await cb.answer()


@router.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    cur.execute("SELECT COUNT(*) FROM users"); users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM videos"); vids = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM videos WHERE category=1"); v1 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM videos WHERE category=2"); v2 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM videos WHERE category=3"); v3 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM payments WHERE status='approved'"); paid = cur.fetchone()[0]
    text = (f"📊 <b>Статистика</b>\n\n"
            f"👥 Юзеров: {users}\n"
            f"🎬 Видео: {vids} (кат1: {v1}, кат2: {v2}, кат3: {v3})\n"
            f"💰 Успешных оплат: {paid}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


# --- РЕКВИЗИТЫ КАРТЫ ---
@router.callback_query(F.data == "adm_card")
async def adm_card(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.clear()
    card = get_setting('card_number')
    cname = get_setting('card_name')
    text = (f"💳 <b>Реквизиты карты</b>\n\n"
            f"Номер: <code>{card}</code>\n"
            f"Имя: <b>{cname}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить номер", callback_data="adm_card_num")],
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="adm_card_name")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "adm_card_num")
async def adm_card_num(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.edit_card_number)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_card")]
    ])
    await cb.message.edit_text("✏️ Отправьте новый номер карты (одним сообщением):", reply_markup=kb)
    await cb.answer()


@router.message(AdmStates.edit_card_number)
async def save_card_num(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    val = (msg.text or '').strip()
    if not val:
        return await msg.answer("❗ Пусто. Введите номер карты.")
    set_setting('card_number', val)
    await state.clear()
    await msg.answer(f"✅ Номер карты обновлён:\n<code>{val}</code>")


@router.callback_query(F.data == "adm_card_name")
async def adm_card_name(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.edit_card_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_card")]
    ])
    await cb.message.edit_text("✏️ Отправьте новое имя владельца карты:", reply_markup=kb)
    await cb.answer()


@router.message(AdmStates.edit_card_name)
async def save_card_name(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    val = (msg.text or '').strip()
    if not val:
        return await msg.answer("❗ Пусто. Введите имя.")
    set_setting('card_name', val)
    await state.clear()
    await msg.answer(f"✅ Имя обновлено: <b>{val}</b>")


# --- КАНАЛ ПОДПИСКИ ---
@router.callback_query(F.data == "adm_channel")
async def adm_channel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.clear()
    enabled = get_setting('sub_enabled', '0') == '1'
    ch_id = get_setting('channel_id') or '—'
    ch_url = get_setting('channel_url') or '—'
    text = (f"📡 <b>Канал подписки</b>\n\n"
            f"Статус: {'✅ Включена' if enabled else '❌ Выключена'}\n"
            f"ID канала: <code>{ch_id}</code>\n"
            f"Ссылка: {ch_url}\n\n"
            f"ℹ️ Бот должен быть <b>админом</b> канала, чтобы проверять подписку.\n"
            f"ID канала — например <code>@mychannel</code> или <code>-1001234567890</code>.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Вкл/Выкл проверку", callback_data="adm_sub_toggle")],
        [InlineKeyboardButton(text="✏️ ID канала", callback_data="adm_sub_id")],
        [InlineKeyboardButton(text="✏️ Ссылка", callback_data="adm_sub_url")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "adm_sub_toggle")
async def adm_sub_toggle(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    cur_state = get_setting('sub_enabled', '0')
    new_state = '0' if cur_state == '1' else '1'
    set_setting('sub_enabled', new_state)
    await cb.answer("✅ Включено" if new_state == '1' else "❌ Выключено", show_alert=True)
    # перерисуем меню
    await adm_channel(cb, None) if False else None
    enabled = new_state == '1'
    ch_id = get_setting('channel_id') or '—'
    ch_url = get_setting('channel_url') or '—'
    text = (f"📡 <b>Канал подписки</b>\n\n"
            f"Статус: {'✅ Включена' if enabled else '❌ Выключена'}\n"
            f"ID канала: <code>{ch_id}</code>\n"
            f"Ссылка: {ch_url}\n\n"
            f"ℹ️ Бот должен быть <b>админом</b> канала, чтобы проверять подписку.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Вкл/Выкл проверку", callback_data="adm_sub_toggle")],
        [InlineKeyboardButton(text="✏️ ID канала", callback_data="adm_sub_id")],
        [InlineKeyboardButton(text="✏️ Ссылка", callback_data="adm_sub_url")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "adm_sub_id")
async def adm_sub_id(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.edit_channel_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_channel")]
    ])
    await cb.message.edit_text(
        "✏️ Отправьте ID канала:\n"
        "• <code>@username</code> — для публичного\n"
        "• <code>-100xxxxxxxxxx</code> — для приватного\n\n"
        "Или отправьте <code>-</code>, чтобы очистить.",
        reply_markup=kb
    )
    await cb.answer()


@router.message(AdmStates.edit_channel_id)
async def save_channel_id(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    val = (msg.text or '').strip()
    if val == '-':
        val = ''
    set_setting('channel_id', val)
    await state.clear()
    await msg.answer(f"✅ ID канала: <code>{val or '—'}</code>")


@router.callback_query(F.data == "adm_sub_url")
async def adm_sub_url(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.edit_channel_url)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_channel")]
    ])
    await cb.message.edit_text(
        "✏️ Отправьте ссылку на канал (для кнопки «Подписаться»):\n"
        "Например: <code>https://t.me/mychannel</code>\n\n"
        "Или <code>-</code>, чтобы очистить.",
        reply_markup=kb
    )
    await cb.answer()


@router.message(AdmStates.edit_channel_url)
async def save_channel_url(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    val = (msg.text or '').strip()
    if val == '-':
        val = ''
    set_setting('channel_url', val)
    await state.clear()
    await msg.answer(f"✅ Ссылка: {val or '—'}")


# --- Рассылка ---
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_back")]
    ])
    await cb.message.edit_text(
        "📢 Отправьте сообщение для рассылки.\nПоддерживается текст, фото, видео, документ с подписью.",
        reply_markup=kb
    )
    await cb.answer()


@router.message(AdmStates.broadcast)
async def do_broadcast(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    cur.execute("SELECT user_id FROM users")
    users = [r[0] for r in cur.fetchall()]
    total = len(users)
    ok = 0
    fail = 0
    status = await msg.answer(f"📢 Начинаю рассылку на {total} пользователей...")

    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=msg.chat.id, message_id=msg.message_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    try:
        await status.edit_text(
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"👥 Всего: {total}\n"
            f"✅ Доставлено: {ok}\n"
            f"❌ Ошибок: {fail}"
        )
    except Exception:
        pass


# --- Поиск юзера ---
@router.callback_query(F.data == "adm_find")
async def adm_find(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.set_state(AdmStates.find_user)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_back")]
    ])
    await cb.message.edit_text("🔍 Отправьте ID пользователя для поиска.", reply_markup=kb)
    await cb.answer()


@router.message(AdmStates.find_user)
async def do_find(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❗ Введи числовой ID.")

    u = get_user(uid)
    if not u:
        return await msg.answer("❌ Юзер не найден.")

    text = (f"👤 <b>Пользователь</b>\n\n"
            f"ID: <code>{u[0]}</code>\n"
            f"Ник: @{u[1] or '—'}\n"
            f"💎 Алмазы: <b>{u[2]}</b>\n"
            f"🚫 Бан: {'Да' if u[3] else 'Нет'}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать алмазы", callback_data=f"adm_gd|{uid}"),
         InlineKeyboardButton(text="➖ Забрать", callback_data=f"adm_td|{uid}")],
        [InlineKeyboardButton(text=f"{'✅ Разбанить' if u[3] else '🚫 Забанить'}", callback_data=f"adm_ban|{uid}")],
        [InlineKeyboardButton(text="🎬 Пак Кат1 (10 видео)", callback_data=f"adm_pk|1|{uid}")],
        [InlineKeyboardButton(text="🎬 Пак Кат2 (15 видео)", callback_data=f"adm_pk|2|{uid}")],
        [InlineKeyboardButton(text="🎁 Пак Кат3 (2 видео)", callback_data=f"adm_pk|3|{uid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    await msg.answer(text, reply_markup=kb)
    await state.clear()


@router.callback_query(F.data.startswith("adm_gd|"))
async def adm_gd(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    uid = int(cb.data.split("|")[1])
    await state.update_data(target=uid, action='give')
    await state.set_state(AdmStates.give_diam)
    await cb.message.answer(f"➕ Сколько алмазов выдать юзеру <code>{uid}</code>?")
    await cb.answer()


@router.callback_query(F.data.startswith("adm_td|"))
async def adm_td(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    uid = int(cb.data.split("|")[1])
    await state.update_data(target=uid, action='take')
    await state.set_state(AdmStates.give_diam)
    await cb.message.answer(f"➖ Сколько алмазов забрать у юзера <code>{uid}</code>?")
    await cb.answer()


@router.message(AdmStates.give_diam)
async def do_give_diam(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        n = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❗ Введи число.")
    data = await state.get_data()
    uid = data['target']
    action = data['action']
    if action == 'give':
        add_diamonds(uid, n)
    else:
        add_diamonds(uid, -n)
    await state.clear()
    u = get_user(uid)
    await msg.answer(f"✅ Готово. У <code>{uid}</code> теперь {u[2]} 💎")
    try:
        if action == 'give':
            await bot.send_message(uid, f"🎉 Вам начислено {n} 💎")
        else:
            await bot.send_message(uid, f"⚠️ С вас списано {n} 💎")
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_ban|"))
async def adm_ban(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    uid = int(cb.data.split("|")[1])
    u = get_user(uid)
    if not u:
        return await cb.answer("Нет юзера", show_alert=True)
    new_state = not bool(u[3])
    set_banned(uid, new_state)
    await cb.answer("Забанен" if new_state else "Разбанен")
    try:
        if new_state:
            await bot.send_message(uid, "🚫 Вы были забанены администратором.")
        else:
            await bot.send_message(uid, "✅ Вы разбанены.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_pk|"))
async def adm_pk(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    _, cat, uid = cb.data.split("|")
    cat = int(cat); uid = int(uid)
    counts = {1: 10, 2: 15, 3: 2}
    n = await deliver(uid, cat, counts[cat])
    await cb.answer(f"✅ Отправлено {n} видео", show_alert=True)
    try:
        await bot.send_message(uid, f"🎁 Админ выдал вам {n} видео (категория {cat}).")
    except Exception:
        pass


# --- Добавить видео ---
@router.callback_query(F.data == "adm_addvideo")
async def adm_addvideo(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Кат 1 — платная (120₽)", callback_data="adm_avc|1")],
        [InlineKeyboardButton(text="Кат 2 — платная (170₽)", callback_data="adm_avc|2")],
        [InlineKeyboardButton(text="Кат 3 — бесплатная (24ч)", callback_data="adm_avc|3")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    await cb.message.edit_text("🎬 Выбери категорию для видео:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("adm_avc|"))
async def adm_avc(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("нет", show_alert=True)
    cat = int(cb.data.split("|")[1])
    await state.update_data(vcat=cat)
    await state.set_state(AdmStates.add_video)
    await cb.message.edit_text(
        f"📤 Отправь видео или документ для категории <b>{cat}</b>.\n"
        f"Можно с подписью.\n\nОтправь /admin чтобы выйти."
    )
    await cb.answer()


@router.message(AdmStates.add_video)
async def do_add_video(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    cat = data.get('vcat', 3)

    if msg.video:
        fid = msg.video.file_id
        ftype = 'video'
    elif msg.document:
        fid = msg.document.file_id
        ftype = 'document'
    else:
        return await msg.answer("❗ Отправь видео или документ.")

    cur.execute(
        "INSERT INTO videos (file_id, file_type, category, caption) VALUES (?,?,?,?)",
        (fid, ftype, cat, msg.caption or '')
    )
    conn.commit()
    await msg.answer(
        f"✅ Видео добавлено в категорию {cat}.\n"
        f"Можешь отправить ещё или /admin для выхода."
    )


# --- Фолбэк ---
@router.message()
async def fallback(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    if is_banned(msg.from_user.id):
        return
    if not await check_sub(msg.from_user.id):
        return await msg.answer("📢 Подпишитесь на канал:", reply_markup=sub_kb())
    await msg.answer("Используйте /start для меню.")


# ==================== ЗАПУСК ====================
async def main():
    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
