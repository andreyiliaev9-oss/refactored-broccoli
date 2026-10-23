import aiosqlite
from datetime import datetime
import pytz

DB_NAME = "prizyva_net.db"
ADMIN_ID = 8141670312
DEFAULT_TZ = "Europe/Moscow"


async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Пользователи (users)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                first_seen TEXT NOT NULL,
                last_contact TEXT NOT NULL,
                operator_notes TEXT DEFAULT ''
            )
        """)

        # 2. Обращения (tickets)
        # Статусы: new, in_progress, waiting_user, pending_verification, closed
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # 3. Сообщения (messages)
        # sender_type: 'user' или 'operator' или 'system'
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_tg_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                text TEXT,
                file_id TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # 4. Напоминания (reminders)
        # Статусы: scheduled, sent, paused_verification, cancelled, error
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                amount TEXT,
                message_text TEXT NOT NULL,
                send_at TEXT NOT NULL,
                periodicity TEXT DEFAULT 'once',
                status TEXT NOT NULL DEFAULT 'scheduled',
                error_details TEXT
            )
        """)

        # 5. Шаблоны (templates)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            )
        """)

        # 6. Рабочее расписание (schedule)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                work_days_text TEXT NOT NULL,
                work_hours_start TEXT NOT NULL,
                work_hours_end TEXT NOT NULL,
                timezone TEXT NOT NULL
            )
        """)

        # 7. Операторы (admin_users)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                tg_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'owner',
                added_at TEXT NOT NULL
            )
        """)

        # 8. Журнал действий (audit_log)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user_tg_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Добавление владельца бота
        await db.execute(
            "INSERT OR IGNORE INTO admin_users (tg_id, role, added_at) VALUES (?, 'owner', ?)",
            (ADMIN_ID, now)
        )

        # Базовый график работы
        await db.execute("""
            INSERT OR IGNORE INTO schedule (id, work_days_text, work_hours_start, work_hours_end, timezone)
            VALUES (1, 'Понедельник — пятница: 09:00–18:00\nСуббота и воскресенье: Выходные дни', '09:00', '18:00', ?)
        """, (DEFAULT_TZ,))

        # Базовые шаблоны (п. 7 ТЗ)
        cursor = await db.execute("SELECT COUNT(*) FROM templates")
        count = (await cursor.fetchone())[0]
        if count == 0:
            default_templates = [
                ("Приветствие", "Первичное уведомление",
                 "Здравствуйте, {имя}! Напоминаем, что данный чат является официальным каналом связи с компанией «Призыва Нет»."),
                ("Напоминание", "Напоминание о платеже",
                 "Уважаемый клиент! Напоминаем о необходимости внести согласованный платёж в размере {сумма} до {дата}. После оплаты, пожалуйста, сообщите об этом в данный чат."),
                ("Изменение условий", "Смена даты или суммы",
                 "Уведомляем вас об изменении параметров платежа. Новая сумма: {сумма}, дата внесения: {дата}."),
                ("Завершение обращения", "Вопрос закрыт",
                 "Ваше обращение №{номер_обращения} рассмотрено и закрыто. При возникновении новых вопросов вы всегда можете написать в этот чат.")
            ]
            await db.executemany(
                "INSERT INTO templates (category, title, body) VALUES (?, ?, ?)",
                default_templates
            )

        await db.commit()


async def log_admin_action(admin_id: int, action: str, target_user_id: int = None, details: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO audit_log (admin_tg_id, action, target_user_tg_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, target_user_id, details, now)
        )
        await db.commit()


async def upsert_user(tg_id: int, username: str, full_name: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users (tg_id, username, full_name, first_seen, last_contact) VALUES (?, ?, ?, ?, ?)",
                (tg_id, username or "", full_name or "Пользователь", now, now)
            )
        else:
            await db.execute(
                "UPDATE users SET username = ?, full_name = ?, last_contact = ? WHERE tg_id = ?",
                (username or "", full_name or "Пользователь", now, tg_id)
            )
        await db.commit()


async def is_working_hours() -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT work_hours_start, work_hours_end, timezone FROM schedule WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return True
        start_str, end_str, tz_name = row

    tz = pytz.timezone(tz_name)
    now_tz = datetime.now(tz)

    # 5 = Суббота, 6 = Воскресенье
    if now_tz.weekday() in (5, 6):
        return False

    current_time = now_tz.strftime("%H:%M")
    return start_str <= current_time <= end_str


async def register_incoming_message(tg_id: int, content_type: str, text: str, file_id: str = None):
    """
    Сохраняет входящее сообщение и управляет тикетом.
    Возвращает кортеж: (ticket_id, need_auto_reply)
    Защита от повторов (п. 3.3): need_auto_reply = True только если создано новое обращение.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, status FROM tickets WHERE user_tg_id = ? AND status != 'closed' ORDER BY id DESC LIMIT 1",
            (tg_id,)
        )
        active_ticket = await cursor.fetchone()

        if active_ticket:
            ticket_id, status = active_ticket
            need_auto_reply = False
            await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
        else:
            cursor = await db.execute(
                "INSERT INTO tickets (user_tg_id, status, created_at, updated_at) VALUES (?, 'new', ?, ?)",
                (tg_id, now, now)
            )
            ticket_id = cursor.lastrowid
            need_auto_reply = True

        await db.execute(
            """INSERT INTO messages (ticket_id, user_tg_id, sender_type, content_type, text, file_id, created_at)
               VALUES (?, ?, 'user', ?, ?, ?, ?)""",
            (ticket_id, tg_id, content_type, text, file_id, now)
        )
        await db.commit()
        return ticket_id, need_auto_reply
