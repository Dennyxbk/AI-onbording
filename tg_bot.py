"""
Telegram-обёртка над пайплайном и маршрутом на pyTelegramBotAPI.   pip install pyTelegramBotAPI

    TG_TOKEN=...  HR_CHAT_ID=...  [SECURITY_CHAT_ID=...]  [REMIND_AT=10:00]  python tg_bot.py

Сотрудник (личка):   вопросы по базе знаний · /route — свой маршрут, отметка этапов, которые можно подтвердить самому
Руководитель (личка): /route — маршруты подчинённых, подтверждение этапов руководителя
Служебные чаты (HR_CHAT_ID / SECURITY_CHAT_ID):
    /status · /metrics · /route <id> · /confirm <id> <этап> [роль] · /vault · /reload · /remind

Эскалации вопросов дублируются в служебные чаты с трассой решения:
    TO_HR → HR_CHAT_ID;  TO_SECURITY и BLOCK → SECURITY_CHAT_ID (если не задан — HR_CHAT_ID)
Напоминания по маршруту рассылаются раз в день в REMIND_AT (местное время сервера).
"""
import logging
import os
import threading
import time
from datetime import date, datetime

import telebot
from telebot import types

import route
from onboarding_pipeline import Pipeline, TOPICS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg_bot")

TOKEN = os.getenv("TG_TOKEN")
if not TOKEN:
    raise SystemExit("Задайте TG_TOKEN (токен от @BotFather) в окружении или в .env")

HR_CHAT = os.getenv("HR_CHAT_ID")
SEC_CHAT = os.getenv("SECURITY_CHAT_ID") or HR_CHAT
ESCALATE_TO = {"TO_HR": HR_CHAT, "TO_SECURITY": SEC_CHAT, "BLOCK": SEC_CHAT}
STAFF_CHATS = {str(c) for c in (HR_CHAT, SEC_CHAT) if c}
REMIND_AT = os.getenv("REMIND_AT", "10:00")

bot = telebot.TeleBot(TOKEN)
pipe = Pipeline()
store = route.Store()

ROUTE_BTN = "📋 Мой маршрут"
HELLO = ("Привет! Я помощник по онбордингу. Отвечаю по базе знаний компании и всегда называю источник, "
         "веду твой маршрут и напоминаю об этапах.\n\n"
         "Спроси про: " + ", ".join(TOPICS) + ".\n"
         "Доступы сверх стандартных и исключения из правил решают люди — я передам им вопрос.")
EXAMPLES = ["Как подключиться к VPN из дома?", "Что взять с собой в первый день?",
            "Когда мне положен первый отпуск?", "Кто мой наставник?"]


def who(user: types.User) -> str:
    return f"@{user.username}" if user.username else f"id{user.id}"


def is_staff(message: types.Message) -> bool:
    return str(message.chat.id) in STAFF_CHATS


def send(chat_id, text, **kw):
    try:
        bot.send_message(chat_id, text, **kw)
        return True
    except Exception:
        log.exception("не удалось отправить в чат %s", chat_id)
        return False


def bind(user: types.User, chat_id: int):
    """Запоминаем chat_id сотрудника и руководителя по username — иначе некому слать напоминания."""
    emp = store.find_by_tg(user.username)
    if emp and store.bind_chat(emp, "chat_id", chat_id):
        log.info("сотрудник %s привязан к чату", emp["id"])
    for e in store.managed_by(user.username):
        store.bind_chat(e, "manager_chat_id", chat_id)


# ---------------------------------------------------------------- базовые команды

@bot.message_handler(commands=["start", "help"])
def start(message: types.Message):
    if is_staff(message):
        bot.reply_to(message, __doc__.split("Служебные чаты")[1].split("\n\n")[0].strip())
        return
    bind(message.from_user, message.chat.id)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton(ROUTE_BTN))
    kb.add(*(types.KeyboardButton(q) for q in EXAMPLES))
    bot.send_message(message.chat.id, HELLO, reply_markup=kb)


@bot.message_handler(commands=["id"])
def chat_id(message: types.Message):
    """Помогает узнать ID служебного чата для HR_CHAT_ID / SECURITY_CHAT_ID."""
    bot.reply_to(message, f"ID этого чата: {message.chat.id}")


# ---------------------------------------------------------------- маршрут

def route_keyboard(emp: dict, role: str, today: date) -> types.InlineKeyboardMarkup | None:
    """Кнопки только для этапов, которые эта роль вправе подтвердить (правило из шаблона)."""
    kb = types.InlineKeyboardMarkup()
    for s in store.route(emp):
        if s["id"] in emp["done"] or role not in s["confirm_by"]:
            continue
        if role == "сотрудник" and today < route.opens(emp, s):
            continue  # этап ещё не начался (например, «Первый день» до выхода)
        prefix = "done" if role == "сотрудник" else "mdone"
        kb.add(types.InlineKeyboardButton(f"✅ {s['title'][:50]}", callback_data=f"{prefix}:{emp['id']}:{s['id']}"))
    return kb if kb.keyboard else None


@bot.message_handler(commands=["route"])
@bot.message_handler(func=lambda m: m.text == ROUTE_BTN)
def show_route(message: types.Message):
    today = date.today()
    if is_staff(message):
        args = message.text.split()[1:]
        if not args:
            bot.reply_to(message, "Формат: /route <id сотрудника>. Список — /status")
            return
        try:
            bot.reply_to(message, route.format_route(store, store.get(args[0]), today))
        except KeyError as e:
            bot.reply_to(message, f"✖ {e}")
        return
    bind(message.from_user, message.chat.id)
    emp = store.find_by_tg(message.from_user.username)
    reports = store.managed_by(message.from_user.username)
    if not emp and not reports:
        bot.reply_to(message, "Не нашёл тебя в списке новых сотрудников. Если ты недавно вышел(ла) — напиши в HR, "
                              "чтобы тебя добавили (нужен твой @username в Telegram).")
        return
    if emp:
        bot.send_message(message.chat.id, route.format_route(store, emp, today, self_view=True),
                         reply_markup=route_keyboard(emp, "сотрудник", today))
    for e in reports:
        bot.send_message(message.chat.id, route.format_route(store, e, today),
                         reply_markup=route_keyboard(e, "руководитель", today))


@bot.callback_query_handler(func=lambda c: c.data.split(":")[0] in ("done", "mdone"))
def on_done(call: types.CallbackQuery):
    kind, emp_id, stage_id = call.data.split(":", 2)
    username = (call.from_user.username or "").lower()
    try:
        emp = store.get(emp_id)
        # роль определяется тем, КТО нажал, а не тем, что написано в кнопке
        if kind == "done" and emp.get("tg", "").lower() == username:
            role = "сотрудник"
        elif kind == "mdone" and emp.get("manager_tg", "").lower() == username:
            role = "руководитель"
        else:
            raise PermissionError("это не твой маршрут")
        stage = store.confirm(emp_id, stage_id, role, who(call.from_user))
    except (KeyError, ValueError, PermissionError) as e:
        bot.answer_callback_query(call.id, f"✖ {e}", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Отмечено ✅")
    today = date.today()
    try:
        bot.edit_message_text(route.format_route(store, emp, today, self_view=role == "сотрудник"),
                              call.message.chat.id, call.message.message_id,
                              reply_markup=route_keyboard(emp, role, today))
    except Exception:
        log.exception("не удалось обновить сообщение")
    if HR_CHAT:
        send(HR_CHAT, f"✅ {emp['name']}: «{stage['title']}» — отметил(а) {who(call.from_user)} ({role})")


# ---------------------------------------------------------------- служебные чаты

def staff_only(handler):
    def wrapper(message):
        if is_staff(message):
            return handler(message)
        bot.reply_to(message, "Эта команда работает только в служебном чате HR/ИБ.")
    return wrapper


@bot.message_handler(commands=["status"])
@staff_only
def status(message):
    bot.reply_to(message, route.format_status(store, date.today()))


@bot.message_handler(commands=["metrics"])
@staff_only
def metrics(message):
    bot.reply_to(message, route.format_metrics(route.metrics(store, date.today())))


@bot.message_handler(commands=["confirm"])
@staff_only
def confirm(message):
    args = message.text.split()[1:]
    if len(args) < 2:
        bot.reply_to(message, "Формат: /confirm <id сотрудника> <id этапа> [роль: hr|it|иб|от|руководитель]. "
                              "id этапов — в /route <id>")
        return
    role = args[2].lower() if len(args) > 2 else "hr"
    if role not in route.STAFF_ROLES:
        bot.reply_to(message, f"✖ роль {role}: допустимы {', '.join(sorted(route.STAFF_ROLES))}")
        return
    try:
        st = store.confirm(args[0], args[1], role, f"{who(message.from_user)} ({role})")
        bot.reply_to(message, f"✅ {args[0]}: «{st['title']}» отмечен")
    except (KeyError, ValueError, PermissionError) as e:
        bot.reply_to(message, f"✖ {e}")


@bot.message_handler(commands=["vault"])
@staff_only
def vault_state(message):
    bot.reply_to(message, vault_report())


@bot.message_handler(commands=["reload"])
@staff_only
def reload_vault(message):
    pipe.reload_vault()
    bot.reply_to(message, "База знаний перечитана.\n" + vault_report())


@bot.message_handler(commands=["remind"])
@staff_only
def remind_now(message):
    n = run_reminders()
    bot.reply_to(message, f"Отправлено напоминаний: {n}")


def vault_report() -> str:
    lines = [f"📚 Заметок в работе: {len(pipe.notes)} из {len(pipe.all_notes)}"]
    for t in pipe.quarantine:
        reasons = "; ".join(f.reason for f in pipe.verdicts[t].findings if f.severity == "block")
        lines.append(f"⛔ {t} — карантин: {reasons}")
    for t in pipe.pending_review:
        lines.append(f"🕓 {t} — ждёт ревью (python vault_guard.py approve \"{t}\" --by ...)")
    return "\n".join(lines)


# ---------------------------------------------------------------- вопросы

@bot.message_handler(content_types=["text"], func=lambda m: m.chat.type == "private")
def on_message(message: types.Message):
    user = who(message.from_user)
    bind(message.from_user, message.chat.id)
    bot.send_chat_action(message.chat.id, "typing")
    try:
        t = pipe.handle(message.text, user=user)
    except Exception:
        log.exception("pipeline failed")
        bot.reply_to(message, "Не получилось обработать вопрос. Я передал его в HR.")
        t = {"route": "TO_HR", "autonomy": "СОВЕТ", "question": message.text, "reason": "ошибка пайплайна"}
    else:
        bot.reply_to(message, t["reply"])

    target = ESCALATE_TO.get(t["route"])
    if target:
        note = (f"⚠️ {t['route']} ({t['autonomy']}) от {user}\n"
                f"«{t['question']}»\n"
                f"Причина: {t['reason']}")
        if t.get("draft_id"):
            note += f"\nЧерновик заявки: {t['draft_id']}"
        if "p_bypass" in t:
            note += (f"\nТрасса: тема={t['topic']} ({t['topic_conf']}), обход={t['p_bypass']}, "
                     f"повыш.права={t['p_elevated']}, исключение={t['p_exception']}")
        send(target, note)


@bot.message_handler(content_types=["photo", "document", "voice", "sticker", "video", "audio"],
                     func=lambda m: m.chat.type == "private")
def not_text(message: types.Message):
    bot.reply_to(message, "Пока я понимаю только текстовые вопросы.")


# ---------------------------------------------------------------- напоминания

def run_reminders(today: date | None = None) -> int:
    sent = 0
    for r in route.due_reminders(store, today or date.today()):
        emp = store.get(r.emp_id)
        chat, prefix = {
            "employee": (emp.get("chat_id"), ""),
            "manager": (emp.get("manager_chat_id"), ""),
            "security": (SEC_CHAT, "ИБ · "),
        }.get(r.to, (HR_CHAT, ""))
        if not chat:  # адресат ещё не написал боту — дублируем в HR, чтобы не потерялось
            chat, prefix = HR_CHAT, f"(для {'сотрудника' if r.to == 'employee' else 'руководителя'}, "\
                                    f"Telegram не подключён) {emp['name']}: "
        if chat and send(chat, prefix + r.text):
            sent += 1
        elif not chat:
            log.warning("некуда отправить напоминание: %s", r)
    log.info("напоминаний отправлено: %d", sent)
    return sent


def reminder_loop():
    """Раз в день в REMIND_AT. Повторы отсекает route.due_reminders (помнит отправленное)."""
    last = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") >= REMIND_AT and last != now.date():
            try:
                run_reminders(now.date())
            except Exception:
                log.exception("ошибка рассылки напоминаний")
            last = now.date()
        time.sleep(60)


if __name__ == "__main__":
    log.info("Бот запущен · роутер: %s · эскалации: HR=%s, ИБ=%s · напоминания в %s",
             pipe.router.name, HR_CHAT, SEC_CHAT, REMIND_AT)
    if pipe.quarantine and SEC_CHAT:
        send(SEC_CHAT, "⛔ При загрузке базы знаний найдены подозрительные заметки:\n" + vault_report())
    threading.Thread(target=reminder_loop, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
