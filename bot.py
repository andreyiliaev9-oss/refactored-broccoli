import asyncio
import logging
import os
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    DB_NAME, ADMIN_ID, init_db, upsert_user, is_working_hours,
    register_incoming_message, log_admin_action
)

# Ваш токен бота
BOT_TOKEN = "8836985962:AAEEjRSTMV0BGVkcQIi1J2Afri1Vz8dfckc"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
router = Router()


# --- ФИЛЬТР ДОСТУПА ОПЕРАТОРА (П. 9 ТЗ) ---
class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id == ADMIN_ID


# --- СОСТОЯНИЯ FSM ДЛЯ ОПЕРАТОРА ---
class OperatorStates(StatesGroup):
    replying_to_user = State()
    direct_msg_user_id = State()
    direct_msg_text = State()
    new_reminder_user = State()
    new_reminder_amount = State()
    new_reminder_date = State()
    new_reminder_text = State()
    edit_schedule = State()


# --- КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ (П. 3.1 - 3.4 ТЗ) ---
def get_user_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Связаться с оператором", callback_data="user_contact_op")],
        [InlineKeyboardButton(text="График работы", callback_data="user_schedule")],
        [InlineKeyboardButton(text="Уведомления", callback_data="user_notifications")],
        [InlineKeyboardButton(text="Информация", callback_data="user_info")]
    ])


def get_schedule_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Написать оператору", callback_data="user_contact_op")],
        [InlineKeyboardButton(text="В главное меню", callback_data="user_main_menu")]
    ])


def get_back_to_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="В главное меню", callback_data="user_main_menu")]
    ])


# --- КЛАВИАТУРЫ АДМИН-ПАНЕЛИ (П. 5 и 8 ТЗ) ---
def get_admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users"),
            InlineKeyboardButton(text="📨 Написать клиенту", callback_data="adm_direct_msg")
        ],
        [
            InlineKeyboardButton(text="📥 Активные обращения", callback_data="adm_tickets"),
            InlineKeyboardButton(text="⏰ Напоминания", callback_data="adm_reminders")
        ],
        [
            InlineKeyboardButton(text="📑 Шаблоны", callback_data="adm_templates"),
            InlineKeyboardButton(text="🕒 Рабочий график", callback_data="adm_schedule")
        ]
    ])


def get_ticket_action_kb(user_tg_id: int, ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ Ответить", callback_data=f"op_reply:{user_tg_id}:{ticket_id}"),
            InlineKeyboardButton(text="📜 История", callback_data=f"op_hist:{user_tg_id}:{ticket_id}")
        ],
        [
            InlineKeyboardButton(text="💳 Ожидает проверки оплаты", callback_data=f"op_verify:{user_tg_id}:{ticket_id}"),
            InlineKeyboardButton(text="✅ Закрыть", callback_data=f"op_close:{user_tg_id}:{ticket_id}")
        ]
    ])


# =====================================================================
# ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ (РАЗДЕЛ 3 ТЗ)
# =====================================================================

WELCOME_TEXT = (
    "Здравствуйте!\n\n"
    "Вы обратились в компанию «Призыва Нет».\n\n"
    "В данном чате вы можете получать уведомления, направлять обращения оператору "
    "и знакомиться с информацией о режиме работы.\n\n"
    "Выберите необходимый раздел в меню ниже."
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(WELCOME_TEXT, reply_markup=get_user_main_kb())


@router.callback_query(F.data == "user_main_menu")
async def cb_user_main_menu(callback: CallbackQuery):
    await callback.message.edit_text(WELCOME_TEXT, reply_markup=get_user_main_kb())
    await callback.answer()


@router.callback_query(F.data == "user_contact_op")
async def cb_user_contact_op(callback: CallbackQuery):
    text = (
        "Вы можете направить сообщение оператору компании «Призыва Нет».\n\n"
        "Пожалуйста, изложите суть вашего вопроса одним сообщением. "
        "Ваше обращение будет зарегистрировано и передано на рассмотрение.\n\n"
        "Ответ поступит в этот чат."
    )
    await callback.message.edit_text(text, reply_markup=get_back_to_main_kb())
    await callback.answer()


@router.callback_query(F.data == "user_schedule")
async def cb_user_schedule(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT work_days_text, timezone FROM schedule WHERE id = 1")
        row = await cursor.fetchone()
        sched_text = row[0] if row else "Понедельник — пятница: 09:00–18:00"
        tz = row[1] if row else "Europe/Moscow"

    text = f"📅 **График работы**\n\n{sched_text}\nЧасовой пояс: {tz}"
    await callback.message.edit_text(text, reply_markup=get_schedule_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "user_notifications")
async def cb_user_notifications(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT amount, send_at, status FROM reminders WHERE user_tg_id = ? AND status = 'scheduled'",
            (callback.from_user.id,)
        )
        rows = await cursor.fetchall()

    if not rows:
        text = "На данный момент у вас нет активных запланированных уведомлений."
    else:
        lines = ["🔔 **Ваши предстоящие уведомления по обязательствам:**\n"]
        for amount, send_at, _ in rows:
            amt_str = f" — Сумма: {amount}" if amount else ""
            lines.append(f"• Дата: {send_at}{amt_str}")
        text = "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=get_back_to_main_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "user_info")
async def cb_user_info(callback: CallbackQuery):
    text = (
        "ℹ️ **Информация**\n\n"
        "• **Назначение бота:** официальный канал информационного взаимодействия "
        "и уведомлений компании «Призыва Нет».\n"
        "• **Порядок связи:** все сообщения, направленные в этот чат, регистрируются "
        "в системе и передаются профильному специалисту.\n"
        "• **Обработка данных:** сведения используются исключительно для исполнения "
        "действующих договоренностей и ведения истории обращений."
    )
    await callback.message.edit_text(text, reply_markup=get_back_to_main_kb(), parse_mode="Markdown")
    await callback.answer()


# =====================================================================
# АДМИНИСТРАТИВНАЯ ПАНЕЛЬ ОПЕРАТОРА (РАЗДЕЛЫ 4, 5, 6, 8 ТЗ)
# =====================================================================

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔒 **Панель управления «Призыва Нет»**\nВыберите необходимый раздел:",
        reply_markup=get_admin_main_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "adm_back", IsAdmin())
async def cb_adm_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🔒 **Панель управления «Призыва Нет»**\nВыберите необходимый раздел:",
        reply_markup=get_admin_main_kb(),
        parse_mode="Markdown"
    )
    await callback.answer()


# --- 1. СПИСОК ПОЛЬЗОВАТЕЛЕЙ ---
@router.callback_query(F.data == "adm_users", IsAdmin())
async def cb_adm_users(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, tg_id, full_name, username, last_contact FROM users ORDER BY last_contact DESC LIMIT 15"
        )
        users = await cursor.fetchall()

    if not users:
        text = "Пользователей в базе пока нет."
    else:
        lines = ["👥 **Последние пользователи (до 15):**\n"]
        for u_id, tg_id, full_name, username, last_c in users:
            uname = f" (@{username})" if username else ""
            lines.append(f"№{u_id} | ID: `{tg_id}` | {full_name}{uname}\nПосл. контакт: {last_c}\n")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить сообщение по ID", callback_data="adm_direct_msg")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- 2. РУЧНАЯ ОТПРАВКА СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЮ (П. 4 ТЗ) ---
@router.callback_query(F.data == "adm_direct_msg", IsAdmin())
async def cb_adm_direct_msg(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OperatorStates.direct_msg_user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_back")]])
    await callback.message.edit_text(
        "Введите **Telegram ID** пользователя, которому нужно отправить сообщение от имени бота:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(OperatorStates.direct_msg_user_id, IsAdmin())
async def process_direct_msg_user_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Пожалуйста, введите корректный числовой Telegram ID.")
        return
    await state.update_data(target_tg_id=int(message.text.strip()))
    await state.set_state(OperatorStates.direct_msg_text)
    await message.answer("Введите текст сообщения (или отправьте документ/фото с подписью):")


@router.message(OperatorStates.direct_msg_text, IsAdmin())
async def process_direct_msg_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_tg_id = data["target_tg_id"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            await bot.send_photo(chat_id=target_tg_id, photo=file_id, caption=caption)
            content_type, text_val = "photo", caption
        elif message.document:
            file_id = message.document.file_id
            caption = message.caption or ""
            await bot.send_document(chat_id=target_tg_id, document=file_id, caption=caption)
            content_type, text_val = "document", caption
        else:
            file_id = None
            text_val = message.text or ""
            await bot.send_message(chat_id=target_tg_id, text=text_val)
            content_type = "text"

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """INSERT INTO messages (ticket_id, user_tg_id, sender_type, content_type, text, file_id, created_at)
                   VALUES (NULL, ?, 'operator', ?, ?, ?, ?)""",
                (target_tg_id, content_type, text_val, file_id, now)
            )
            await db.commit()

        await log_admin_action(ADMIN_ID, "direct_message", target_tg_id, text_val[:100])
        await message.answer(
            f"✅ Сообщение успешно доставлено пользователю `{target_tg_id}` от имени бота.",
            reply_markup=get_admin_main_kb(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка доставки пользователю {target_tg_id}: {e}", reply_markup=get_admin_main_kb())
    finally:
        await state.clear()


# --- 3. ОБРАБОТКА ОБРАЩЕНИЙ И ОТВЕТ ОПЕРАТОРА (П. 5.1 - 5.2 ТЗ) ---
@router.callback_query(F.data == "adm_tickets", IsAdmin())
async def cb_adm_tickets(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, user_tg_id, status, updated_at FROM tickets WHERE status != 'closed' ORDER BY updated_at DESC LIMIT 10"
        )
        tickets = await cursor.fetchall()

    if not tickets:
        await callback.message.edit_text(
            "Активных обращений нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]])
        )
        await callback.answer()
        return

    lines = ["📥 **Открытые обращения:**\n"]
    kb_rows = []
    for t_id, u_id, status, upd in tickets:
        lines.append(f"• Обращение №{t_id} | ID `{u_id}` | Статус: `{status}` ({upd})")
        kb_rows.append([
            InlineKeyboardButton(text=f"Открыть №{t_id} (ID {u_id})", callback_data=f"op_hist:{u_id}:{t_id}")
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("op_reply:"), IsAdmin())
async def cb_op_reply(callback: CallbackQuery, state: FSMContext):
    _, user_tg_id, ticket_id = callback.data.split(":")
    await state.set_state(OperatorStates.replying_to_user)
    await state.update_data(target_tg_id=int(user_tg_id), ticket_id=int(ticket_id))
    await callback.message.answer(
        f"✍️ Введите ваш ответ по обращению №{ticket_id} для пользователя `{user_tg_id}` (можно прикрепить фото или документ):",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(OperatorStates.replying_to_user, IsAdmin())
async def process_operator_reply(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_tg_id = data["target_tg_id"]
    ticket_id = data["ticket_id"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            await bot.send_photo(chat_id=target_tg_id, photo=file_id, caption=caption)
            content_type, text_val = "photo", caption
        elif message.document:
            file_id = message.document.file_id
            caption = message.caption or ""
            await bot.send_document(chat_id=target_tg_id, document=file_id, caption=caption)
            content_type, text_val = "document", caption
        else:
            file_id = None
            text_val = message.text or ""
            await bot.send_message(chat_id=target_tg_id, text=text_val)
            content_type = "text"

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE tickets SET status = 'waiting_user', updated_at = ? WHERE id = ?",
                (now, ticket_id)
            )
            await db.execute(
                """INSERT INTO messages (ticket_id, user_tg_id, sender_type, content_type, text, file_id, created_at)
                   VALUES (?, ?, 'operator', ?, ?, ?, ?)""",
                (ticket_id, target_tg_id, content_type, text_val, file_id, now)
            )
            await db.commit()

        await log_admin_action(ADMIN_ID, "reply_ticket", target_tg_id, f"Ticket #{ticket_id}")
        await message.answer(f"✅ Ответ по обращению №{ticket_id} отправлен пользователю `{target_tg_id}`.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить ответ: {e}")
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("op_hist:"), IsAdmin())
async def cb_op_history(callback: CallbackQuery):
    _, user_tg_id, ticket_id = callback.data.split(":")
    user_tg_id, ticket_id = int(user_tg_id), int(ticket_id)

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT sender_type, text, created_at FROM messages WHERE user_tg_id = ? ORDER BY id DESC LIMIT 10",
            (user_tg_id,)
        )
        msgs = await cursor.fetchall()

    if not msgs:
        hist_text = "История сообщений пуста."
    else:
        lines = [f"📜 **Последние сообщения (Пользователь `{user_tg_id}`):**\n"]
        for sender, txt, dt in reversed(msgs):
            role = "👤 Клиент" if sender == "user" else "🛡 Оператор"
            lines.append(f"[{dt}] **{role}:**\n{txt or '[Вложение]'}\n")
        hist_text = "\n".join(lines)

    await callback.message.answer(
        hist_text,
        reply_markup=get_ticket_action_kb(user_tg_id, ticket_id),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("op_verify:"), IsAdmin())
async def cb_op_verify_payment(callback: CallbackQuery):
    _, user_tg_id, ticket_id = callback.data.split(":")
    user_tg_id, ticket_id = int(user_tg_id), int(ticket_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tickets SET status = 'pending_verification', updated_at = ? WHERE id = ?", (now, ticket_id))
        await db.execute(
            "UPDATE reminders SET status = 'paused_verification' WHERE user_tg_id = ? AND status = 'scheduled'",
            (user_tg_id,)
        )
        await db.commit()

    await log_admin_action(ADMIN_ID, "set_pending_verification", user_tg_id, f"Ticket #{ticket_id}")
    await callback.message.answer(
        f"💳 Обращение №{ticket_id} переведено в статус **«Ожидает проверки»**.\n"
        f"Автоматические напоминания для пользователя `{user_tg_id}` приостановлены до подтверждения.",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("op_close:"), IsAdmin())
async def cb_op_close(callback: CallbackQuery, bot: Bot):
    _, user_tg_id, ticket_id = callback.data.split(":")
    user_tg_id, ticket_id = int(user_tg_id), int(ticket_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?", (now, ticket_id))
        await db.commit()

    await log_admin_action(ADMIN_ID, "close_ticket", user_tg_id, f"Ticket #{ticket_id}")
    await callback.message.answer(f"✅ Обращение №{ticket_id} закрыто.")
    await callback.answer()


# --- 4. УПРАВЛЕНИЕ НАПОМИНАНИЯМИ О ПЛАТЕЖАХ (П. 6 ТЗ) ---
@router.callback_query(F.data == "adm_reminders", IsAdmin())
async def cb_adm_reminders(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, user_tg_id, amount, send_at, status FROM reminders ORDER BY id DESC LIMIT 10"
        )
        rows = await cursor.fetchall()

    lines = ["⏰ **Запланированные и недавние напоминания:**\n"]
    if not rows:
        lines.append("Список напоминаний пуст.")
    else:
        for r_id, u_id, amt, s_at, st in rows:
            lines.append(f"№{r_id} | ID `{u_id}` | {amt} | Дата: {s_at} | Статус: `{st}`")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать напоминание", callback_data="adm_add_reminder")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "adm_add_reminder", IsAdmin())
async def cb_adm_add_reminder(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OperatorStates.new_reminder_user)
    await callback.message.edit_text("Введите **Telegram ID** получателя напоминания:", parse_mode="Markdown")
    await callback.answer()


@router.message(OperatorStates.new_reminder_user, IsAdmin())
async def process_rem_user(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Введите корректный числовой Telegram ID.")
        return
    await state.update_data(rem_user=int(message.text.strip()))
    await state.set_state(OperatorStates.new_reminder_amount)
    await message.answer("Введите сумму платежа (например: `30 000 ₽`):", parse_mode="Markdown")


@router.message(OperatorStates.new_reminder_amount, IsAdmin())
async def process_rem_amount(message: Message, state: FSMContext):
    await state.update_data(rem_amount=message.text.strip())
    await state.set_state(OperatorStates.new_reminder_date)
    await message.answer(
        "Введите дату и время отправки в формате `ГГГГ-ММ-ДД ЧЧ:ММ` (например, `2026-10-25 12:00`):",
        parse_mode="Markdown"
    )


@router.message(OperatorStates.new_reminder_date, IsAdmin())
async def process_rem_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("Неверный формат. Используйте формат `ГГГГ-ММ-ДД ЧЧ:ММ`, например: `2026-10-25 12:00`", parse_mode="Markdown")
        return

    await state.update_data(rem_date=date_str)
    await state.set_state(OperatorStates.new_reminder_text)
    await message.answer(
        "Введите текст напоминания (или отправьте `+`, чтобы использовать стандартный официальный шаблон с указанной суммой и датой):"
    )


@router.message(OperatorStates.new_reminder_text, IsAdmin())
async def process_rem_text(message: Message, state: FSMContext):
    data = await state.get_data()
    u_id = data["rem_user"]
    amt = data["rem_amount"]
    dt = data["rem_date"]

    if message.text.strip() == "+":
        msg_text = (
            f"Уважаемый клиент!\n\n"
            f"Напоминаем о необходимости внести согласованный платёж по обязательствам.\n"
            f"Сумма к внесению: {amt}\n"
            f"Дата: {dt}\n\n"
            f"После совершения платежа, пожалуйста, сообщите об этом в данный чат."
        )
    else:
        msg_text = message.text.strip()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO reminders (user_tg_id, amount, message_text, send_at, status)
               VALUES (?, ?, ?, ?, 'scheduled')""",
            (u_id, amt, msg_text, dt)
        )
        await db.commit()

    await log_admin_action(ADMIN_ID, "create_reminder", u_id, f"{amt} at {dt}")
    await state.clear()
    await message.answer(
        f"✅ **Карточка напоминания создана:**\n\n"
        f"Получатель: ID `{u_id}`\n"
        f"Сумма: {amt}\n"
        f"Дата отправки: {dt}\n"
        f"Статус: Запланировано",
        reply_markup=get_admin_main_kb(),
        parse_mode="Markdown"
    )


# --- 5. ШАБЛОНЫ И РЕДАКТИРОВАНИЕ ГРАФИКА (П. 3.4 и 7 ТЗ) ---
@router.callback_query(F.data == "adm_templates", IsAdmin())
async def cb_adm_templates(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT category, title, body FROM templates")
        rows = await cursor.fetchall()

    lines = ["📑 **Доступные шаблоны сообщений:**\n"]
    for cat, title, body in rows:
        lines.append(f"🔹 **{cat} — {title}**\n`{body}`\n")

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "adm_schedule", IsAdmin())
async def cb_adm_schedule(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OperatorStates.edit_schedule)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_back")]])
    await callback.message.edit_text(
        "Отправьте новый текст графика работы и часы в трех строках:\n"
        "1 строка: Время начала (например, `09:00`)\n"
        "2 строка: Время окончания (например, `18:00`)\n"
        "3 строка и далее: Описание графика для пользователя.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(OperatorStates.edit_schedule, IsAdmin())
async def process_edit_schedule(message: Message, state: FSMContext):
    parts = message.text.strip().split("\n", 2)
    if len(parts) < 3:
        await message.answer("Пожалуйста, укажите данные минимум в 3 строки (начало, конец, описание).")
        return

    start_h, end_h, desc = parts[0].strip(), parts[1].strip(), parts[2].strip()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE schedule SET work_hours_start = ?, work_hours_end = ?, work_days_text = ? WHERE id = 1",
            (start_h, end_h, desc)
        )
        await db.commit()

    await log_admin_action(ADMIN_ID, "update_schedule", None, f"{start_h}-{end_h}")
    await state.clear()
    await message.answer("✅ График работы успешно обновлён.", reply_markup=get_admin_main_kb())


# =====================================================================
# ПРИЁМ ВХОДЯЩИХ СООБЩЕНИЙ ОТ ПОЛЬЗОВАТЕЛЕЙ (П. 3.2, 3.3, 5.1, 12А/В)
# =====================================================================

@router.message(~IsAdmin())
async def handle_user_incoming(message: Message, bot: Bot):
    tg_id = message.from_user.id
    await upsert_user(tg_id, message.from_user.username, message.from_user.full_name)

    if message.photo:
        content_type = "photo"
        file_id = message.photo[-1].file_id
        text_content = message.caption or "[Фотография без подписи]"
    elif message.document:
        content_type = "document"
        file_id = message.document.file_id
        text_content = message.caption or f"[Документ: {message.document.file_name}]"
    else:
        content_type = "text"
        file_id = None
        text_content = message.text or "[Сообщение]"

    ticket_id, need_auto_reply = await register_incoming_message(tg_id, content_type, text_content, file_id)

    if need_auto_reply:
        reply_text = (
            "Ваше сообщение принято и передано оператору компании «Призыва Нет».\n\n"
            "Специалист рассмотрит обращение в соответствии с графиком работы.\n\n"
            "Ответ будет направлен в этот чат."
        )
        if not await is_working_hours():
            reply_text += (
                "\n\n🕒 Обратите внимание: ваше сообщение поступило в нерабочее время. "
                "Оператор ознакомится с ним в ближайшие рабочие часы."
            )
        await message.answer(reply_text)

    now_time = datetime.now().strftime("%H:%M")
    admin_notify_text = (
        f"🔔 **Новое обращение №{ticket_id}**\n"
        f"Статус: Новое\n"
        f"Пользователь: ID `{tg_id}` ({message.from_user.full_name})\n"
        f"Время: {now_time}\n\n"
        f"Сообщение пользователя:\n{text_content}"
    )

    kb = get_ticket_action_kb(tg_id, ticket_id)
    if content_type == "photo":
        await bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=admin_notify_text, reply_markup=kb, parse_mode="Markdown")
    elif content_type == "document":
        await bot.send_document(chat_id=ADMIN_ID, document=file_id, caption=admin_notify_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_notify_text, reply_markup=kb, parse_mode="Markdown")


# =====================================================================
# ПЛАНИРОВЩИК АВТОМАТИЧЕСКИХ НАПОМИНАНИЙ (П. 6 и 12Б ТЗ)
# =====================================================================

async def check_and_send_reminders(bot: Bot):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, user_tg_id, amount, message_text FROM reminders WHERE status = 'scheduled' AND send_at <= ?",
            (now_str,)
        )
        due_reminders = await cursor.fetchall()

        for r_id, user_tg_id, amount, msg_text in due_reminders:
            try:
                await bot.send_message(chat_id=user_tg_id, text=msg_text)
                await db.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,))
                await db.execute(
                    """INSERT INTO messages (ticket_id, user_tg_id, sender_type, content_type, text, created_at)
                       VALUES (NULL, ?, 'system', 'text', ?, ?)""",
                    (user_tg_id, f"[Авто-напоминание]: {msg_text}", now_str)
                )
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"✅ Напоминание №{r_id} (сумма: {amount}) успешно доставлено пользователю `{user_tg_id}`.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                err_msg = str(e)[:200]
                await db.execute(
                    "UPDATE reminders SET status = 'error', error_details = ? WHERE id = ?",
                    (err_msg, r_id)
                )
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Ошибка отправки напоминания №{r_id} пользователю `{user_tg_id}`: {err_msg}",
                    parse_mode="Markdown"
                )
        await db.commit()


# =====================================================================
# ТОЧКА ВХОДА (С ПРОКСИ ДЛЯ PYTHONANYWHERE)
# =====================================================================

async def main():
    await init_db()

    # Подключение через прокси PythonAnywhere
    session = AiohttpSession(proxy="http://proxy.server:3128")
    bot = Bot(token=BOT_TOKEN, session=session)

    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_and_send_reminders, "interval", minutes=1, kwargs={"bot": bot})
    scheduler.start()

    logging.info("Бот «Призыва Нет» запущен. Оператор: %s", ADMIN_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())