#!/usr/bin/env python3
from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Callable

import telebot
from mcrcon import MCRcon
from telebot import types


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    service_name: str
    rcon_host: str
    rcon_port: int
    rcon_password: str
    console_lines: int
    audit_log: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        raw_admins = os.getenv("ADMIN_IDS", "").strip()
        service = os.getenv("MC_SERVICE", "server2-vanilla.service").strip()
        rcon_host = os.getenv("RCON_HOST", "127.0.0.1").strip()
        rcon_password = os.getenv("RCON_PASSWORD", "")
        audit_log = os.getenv("AUDIT_LOG", "/var/lib/mc-admin-bot/audit.log").strip()

        if not token:
            raise RuntimeError("BOT_TOKEN is not set")
        if not raw_admins:
            raise RuntimeError("ADMIN_IDS is not set")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
            raise RuntimeError("MC_SERVICE contains invalid characters")

        try:
            admin_ids = frozenset(int(item.strip()) for item in raw_admins.split(",") if item.strip())
            rcon_port = int(os.getenv("RCON_PORT", "25578"))
            console_lines = int(os.getenv("CONSOLE_LINES", "40"))
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS, RCON_PORT or CONSOLE_LINES has an invalid value") from exc

        if not admin_ids or any(admin_id <= 0 for admin_id in admin_ids):
            raise RuntimeError("ADMIN_IDS must contain positive Telegram numeric IDs")
        if not (1 <= rcon_port <= 65535):
            raise RuntimeError("RCON_PORT must be between 1 and 65535")
        if not (5 <= console_lines <= 100):
            raise RuntimeError("CONSOLE_LINES must be between 5 and 100")
        if not rcon_password:
            raise RuntimeError("RCON_PASSWORD is not set")

        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            service_name=service,
            rcon_host=rcon_host,
            rcon_port=rcon_port,
            rcon_password=rcon_password,
            console_lines=console_lines,
            audit_log=audit_log,
        )


CFG = Config.from_env()

log_dir = os.path.dirname(CFG.audit_log)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

logger = logging.getLogger("mc-admin-bot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
file_handler = RotatingFileHandler(CFG.audit_log, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

bot = telebot.TeleBot(CFG.bot_token, parse_mode="HTML", threaded=True, num_threads=4)
pending_console_input: dict[int, float] = {}


class CommandError(RuntimeError):
    pass


def run_process(args: list[str], timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError("Команда на сервере превысила время ожидания") from exc
    except OSError as exc:
        raise CommandError(f"Не удалось запустить системную команду: {exc}") from exc

    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise CommandError(output or f"Системная команда завершилась с кодом {result.returncode}")
    return output


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in CFG.admin_ids


def actor_text(user: types.User | None) -> str:
    if user is None:
        return "unknown"
    username = f"@{user.username}" if user.username else "no_username"
    return f"id={user.id} user={username} name={user.first_name!r}"


def require_admin(handler: Callable):
    @wraps(handler)
    def wrapped(message: types.Message, *args, **kwargs):
        user_id = message.from_user.id if message.from_user else None
        if message.chat.type != "private":
            bot.reply_to(message, "⛔ Управление сервером разрешено только в личных сообщениях с ботом.")
            return None
        if not is_admin(user_id):
            logger.warning("DENIED message %s", actor_text(message.from_user))
            bot.reply_to(
                message,
                "⛔ <b>Доступ запрещён.</b>\n"
                f"Твой Telegram ID: <code>{user_id}</code>",
            )
            return None
        return handler(message, *args, **kwargs)

    return wrapped


def require_admin_callback(handler: Callable):
    @wraps(handler)
    def wrapped(call: types.CallbackQuery, *args, **kwargs):
        if not is_admin(call.from_user.id):
            logger.warning("DENIED callback %s data=%r", actor_text(call.from_user), call.data)
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return None
        if call.message and call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "Только в личном чате", show_alert=True)
            return None
        return handler(call, *args, **kwargs)

    return wrapped


def main_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("📊 Статус", callback_data="status"),
        types.InlineKeyboardButton("👥 Игроки", callback_data="players"),
    )
    keyboard.add(
        types.InlineKeyboardButton("▶️ Запустить", callback_data="start"),
        types.InlineKeyboardButton("⏹ Остановить", callback_data="ask_stop"),
    )
    keyboard.add(
        types.InlineKeyboardButton("🔄 Перезапустить", callback_data="ask_restart"),
        types.InlineKeyboardButton("🖥 Консоль", callback_data="console"),
    )
    keyboard.add(types.InlineKeyboardButton("⌨️ Ввести команду", callback_data="command_mode"))
    return keyboard


def confirmation_keyboard(action: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ Да", callback_data=f"confirm_{action}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    )
    return keyboard


def console_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="console"),
        types.InlineKeyboardButton("⌨️ Команда", callback_data="command_mode"),
    )
    keyboard.add(types.InlineKeyboardButton("◀️ Меню", callback_data="menu"))
    return keyboard


def trim_for_telegram(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return "…\n" + text[-(limit - 2):]


def code_message(title: str, content: str) -> str:
    safe_title = html.escape(title[:300])
    safe_content = html.escape(content or "(пусто)")
    overhead = len(safe_title) + len("<b></b>\n<pre></pre>")
    budget = max(300, 4096 - overhead - 20)
    if len(safe_content) > budget:
        safe_content = "…\n" + safe_content[-(budget - 2):]
    return f"<b>{safe_title}</b>\n<pre>{safe_content}</pre>"


def send_code(chat_id: int, title: str, content: str, reply_markup=None) -> None:
    bot.send_message(chat_id, code_message(title, content), reply_markup=reply_markup)


def edit_or_send(call: types.CallbackQuery, text: str, reply_markup=None) -> None:
    if not call.message:
        return
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=reply_markup,
        )
    except telebot.apihelper.ApiTelegramException as exc:
        if "message is not modified" not in str(exc).lower():
            bot.send_message(call.message.chat.id, text, reply_markup=reply_markup)


def systemctl_action(action: str) -> str:
    if action not in {"start", "stop", "restart"}:
        raise CommandError("Недопустимое действие")
    return run_process(["sudo", "-n", "/usr/bin/systemctl", action, CFG.service_name], timeout=90)


def service_properties() -> dict[str, str]:
    fields = [
        "ActiveState",
        "SubState",
        "ActiveEnterTimestamp",
        "MainPID",
        "MemoryCurrent",
        "CPUUsageNSec",
        "NRestarts",
    ]
    output = run_process(
        ["/usr/bin/systemctl", "show", CFG.service_name, "--no-pager", "--property=" + ",".join(fields)],
        timeout=10,
    )
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    return properties


def format_bytes(raw: str) -> str:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return "—"
    if value < 0 or value >= 2**63 - 1:
        return "—"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return "—"


def format_cpu_ns(raw: str) -> str:
    try:
        seconds = int(raw) / 1_000_000_000
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} сек"
    minutes, second = divmod(int(seconds), 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minute} мин"
    return f"{minute} мин {second} сек"


def rcon_command(command: str) -> str:
    command = command.strip().lstrip("/").strip()
    if not command:
        raise CommandError("Пустая команда")
    if "\n" in command or "\r" in command:
        raise CommandError("Команда должна быть в одну строку")
    if len(command) > 300:
        raise CommandError("Команда слишком длинная: максимум 300 символов")

    try:
        with MCRcon(
            CFG.rcon_host,
            CFG.rcon_password,
            port=CFG.rcon_port,
            timeout=7,
        ) as client:
            response = client.command(command)
    except Exception as exc:
        raise CommandError(f"RCON недоступен: {exc}") from exc
    return response.strip() if response else ""


def status_text() -> str:
    properties = service_properties()
    active = properties.get("ActiveState", "unknown")
    sub = properties.get("SubState", "unknown")
    icon = "🟢" if active == "active" else "🔴" if active in {"inactive", "failed"} else "🟡"
    started = properties.get("ActiveEnterTimestamp") or "—"
    pid = properties.get("MainPID", "0")
    if pid == "0":
        pid = "—"

    rcon_line = "⚪ недоступен"
    players_line = "—"
    if active == "active":
        try:
            players_line = rcon_command("list") or "Команда list выполнена без ответа"
            rcon_line = "🟢 доступен"
        except CommandError as exc:
            rcon_line = f"🔴 {exc}"

    return (
        f"<b>Сервер Minecraft</b>\n"
        f"{icon} Сервис: <code>{html.escape(active)}/{html.escape(sub)}</code>\n"
        f"🧩 Unit: <code>{html.escape(CFG.service_name)}</code>\n"
        f"🆔 PID: <code>{html.escape(pid)}</code>\n"
        f"🕒 Запущен: <code>{html.escape(started)}</code>\n"
        f"🧠 Память: <code>{html.escape(format_bytes(properties.get('MemoryCurrent', '')))}</code>\n"
        f"⚙️ CPU-время: <code>{html.escape(format_cpu_ns(properties.get('CPUUsageNSec', '')))}</code>\n"
        f"♻️ Рестартов: <code>{html.escape(properties.get('NRestarts', '0'))}</code>\n"
        f"🔌 RCON: {html.escape(rcon_line)}\n"
        f"👥 <code>{html.escape(trim_for_telegram(players_line, 500))}</code>"
    )


def console_output(lines: int | None = None) -> str:
    count = lines or CFG.console_lines
    count = max(5, min(count, 100))
    return run_process(
        [
            "/usr/bin/journalctl",
            "-u",
            CFG.service_name,
            "-n",
            str(count),
            "--no-pager",
            "--output=cat",
        ],
        timeout=15,
    )


def parse_console_lines(message: types.Message) -> int:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        return CFG.console_lines
    try:
        return max(5, min(int(parts[1]), 100))
    except ValueError as exc:
        raise CommandError("Использование: /console 40 (от 5 до 100 строк)") from exc


def audit(user: types.User | None, action: str, details: str = "") -> None:
    safe_details = details.replace("\n", " ")[:500]
    logger.info("ACTION %s action=%s details=%r", actor_text(user), action, safe_details)


@bot.message_handler(commands=["id"])
def command_id(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    bot.reply_to(message, f"Твой Telegram ID: <code>{user_id}</code>")


@bot.message_handler(commands=["start", "menu", "help"])
@require_admin
def command_start(message: types.Message) -> None:
    text = (
        "<b>🎮 Minecraft Admin Bot</b>\n\n"
        "Управление доступно только администраторам из <code>ADMIN_IDS</code>.\n\n"
        "Команды:\n"
        "<code>/status</code> — состояние сервера\n"
        "<code>/console 40</code> — последние 5–100 строк\n"
        "<code>/cmd list</code> — команда Minecraft через RCON\n"
        "<code>/players</code> — список игроков\n"
        "<code>/say текст</code> — сообщение на сервер\n"
        "<code>/start_server</code>, <code>/stop_server</code>, <code>/restart_server</code>"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())


@bot.message_handler(commands=["status"])
@require_admin
def command_status(message: types.Message) -> None:
    try:
        bot.send_message(message.chat.id, status_text(), reply_markup=main_keyboard())
    except CommandError as exc:
        bot.reply_to(message, f"❌ <b>Ошибка статуса:</b> <code>{html.escape(str(exc))}</code>")


@bot.message_handler(commands=["console"])
@require_admin
def command_console(message: types.Message) -> None:
    try:
        lines = parse_console_lines(message)
        audit(message.from_user, "console", f"lines={lines}")
        send_code(message.chat.id, f"Последние {lines} строк консоли", console_output(lines), console_keyboard())
    except CommandError as exc:
        bot.reply_to(message, f"❌ <code>{html.escape(str(exc))}</code>")


@bot.message_handler(commands=["cmd"])
@require_admin
def command_cmd(message: types.Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Использование: <code>/cmd list</code>")
        return
    command = parts[1]
    try:
        audit(message.from_user, "rcon", command)
        result = rcon_command(command)
        send_code(
            message.chat.id,
            f"> {command}",
            result or "Команда выполнена без текстового ответа",
            main_keyboard(),
        )
    except CommandError as exc:
        bot.reply_to(message, f"❌ <b>RCON:</b> <code>{html.escape(str(exc))}</code>")


@bot.message_handler(commands=["players"])
@require_admin
def command_players(message: types.Message) -> None:
    try:
        audit(message.from_user, "players")
        send_code(message.chat.id, "Игроки", rcon_command("list"), main_keyboard())
    except CommandError as exc:
        bot.reply_to(message, f"❌ <b>RCON:</b> <code>{html.escape(str(exc))}</code>")


@bot.message_handler(commands=["say"])
@require_admin
def command_say(message: types.Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Использование: <code>/say Сервер перезапустится через 5 минут</code>")
        return
    text = parts[1].strip()
    try:
        audit(message.from_user, "say", text)
        result = rcon_command(f"say {text}")
        send_code(message.chat.id, "Сообщение отправлено", result or "Готово", main_keyboard())
    except CommandError as exc:
        bot.reply_to(message, f"❌ <b>RCON:</b> <code>{html.escape(str(exc))}</code>")


@bot.message_handler(commands=["start_server"])
@require_admin
def command_start_server(message: types.Message) -> None:
    perform_system_action(message.chat.id, message.from_user, "start")


@bot.message_handler(commands=["stop_server"])
@require_admin
def command_stop_server(message: types.Message) -> None:
    bot.send_message(message.chat.id, "Остановить Minecraft-сервер?", reply_markup=confirmation_keyboard("stop"))


@bot.message_handler(commands=["restart_server"])
@require_admin
def command_restart_server(message: types.Message) -> None:
    bot.send_message(message.chat.id, "Перезапустить Minecraft-сервер?", reply_markup=confirmation_keyboard("restart"))


def perform_system_action(chat_id: int, user: types.User | None, action: str) -> None:
    labels = {"start": "запущен", "stop": "остановлен", "restart": "перезапущен"}
    try:
        audit(user, f"systemctl_{action}", CFG.service_name)
        systemctl_action(action)
        time.sleep(1)
        bot.send_message(
            chat_id,
            f"✅ Сервер {labels[action]}.\n\n{status_text()}",
            reply_markup=main_keyboard(),
        )
    except CommandError as exc:
        logger.error("systemctl action failed: action=%s error=%s", action, exc)
        bot.send_message(
            chat_id,
            f"❌ <b>Ошибка:</b> <code>{html.escape(str(exc))}</code>",
            reply_markup=main_keyboard(),
        )


@bot.callback_query_handler(func=lambda call: True)
@require_admin_callback
def callbacks(call: types.CallbackQuery) -> None:
    data = call.data or ""
    bot.answer_callback_query(call.id)

    if data == "menu":
        edit_or_send(call, "<b>🎮 Управление Minecraft-сервером</b>", main_keyboard())
        return
    if data == "status":
        try:
            edit_or_send(call, status_text(), main_keyboard())
        except CommandError as exc:
            edit_or_send(call, f"❌ <code>{html.escape(str(exc))}</code>", main_keyboard())
        return
    if data == "players":
        try:
            audit(call.from_user, "players")
            send_code(call.message.chat.id, "Игроки", rcon_command("list"), main_keyboard())
        except CommandError as exc:
            bot.send_message(call.message.chat.id, f"❌ <b>RCON:</b> <code>{html.escape(str(exc))}</code>")
        return
    if data == "console":
        try:
            audit(call.from_user, "console", f"lines={CFG.console_lines}")
            output = console_output(CFG.console_lines)
            edit_or_send(
                call,
                code_message(f"Последние {CFG.console_lines} строк консоли", output),
                console_keyboard(),
            )
        except CommandError as exc:
            edit_or_send(call, f"❌ <code>{html.escape(str(exc))}</code>", console_keyboard())
        return
    if data == "command_mode":
        pending_console_input[call.from_user.id] = time.monotonic() + 120
        bot.send_message(
            call.message.chat.id,
            "⌨️ Отправь следующей строкой Minecraft-команду без <code>/</code>.\n"
            "Например: <code>say Привет</code> или <code>whitelist add Nick</code>.\n"
            "Режим отменится через 2 минуты. Для отмены: <code>отмена</code>.",
        )
        return
    if data == "start":
        perform_system_action(call.message.chat.id, call.from_user, "start")
        return
    if data == "ask_stop":
        edit_or_send(call, "⚠️ <b>Точно остановить Minecraft-сервер?</b>", confirmation_keyboard("stop"))
        return
    if data == "ask_restart":
        edit_or_send(call, "⚠️ <b>Точно перезапустить Minecraft-сервер?</b>", confirmation_keyboard("restart"))
        return
    if data == "confirm_stop":
        perform_system_action(call.message.chat.id, call.from_user, "stop")
        return
    if data == "confirm_restart":
        perform_system_action(call.message.chat.id, call.from_user, "restart")
        return
    if data == "cancel":
        edit_or_send(call, "Действие отменено.", main_keyboard())
        return


@bot.message_handler(content_types=["text"], func=lambda message: True)
def pending_text(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_admin(user_id) or message.chat.type != "private":
        if message.text and not message.text.startswith("/"):
            logger.warning("DENIED plain text %s", actor_text(message.from_user))
        return

    expires = pending_console_input.get(user_id)
    if expires is None:
        return
    if time.monotonic() > expires:
        pending_console_input.pop(user_id, None)
        bot.reply_to(message, "⌛ Режим ввода команды истёк. Нажми «Ввести команду» ещё раз.")
        return

    pending_console_input.pop(user_id, None)
    command = (message.text or "").strip()
    if command.lower() in {"отмена", "cancel"}:
        bot.reply_to(message, "Отменено.", reply_markup=main_keyboard())
        return

    try:
        audit(message.from_user, "rcon", command)
        result = rcon_command(command)
        send_code(
            message.chat.id,
            f"> {command}",
            result or "Команда выполнена без текстового ответа",
            main_keyboard(),
        )
    except CommandError as exc:
        bot.reply_to(
            message,
            f"❌ <b>RCON:</b> <code>{html.escape(str(exc))}</code>",
            reply_markup=main_keyboard(),
        )


def notify_admins_startup() -> None:
    for admin_id in CFG.admin_ids:
        try:
            bot.send_message(admin_id, "🤖 Minecraft Admin Bot запущен.", reply_markup=main_keyboard())
        except Exception as exc:
            logger.warning("Could not notify admin id=%s: %s", admin_id, exc)


def main() -> None:
    bot.set_my_commands(
        [
            types.BotCommand("start", "Открыть меню"),
            types.BotCommand("status", "Статус Minecraft-сервера"),
            types.BotCommand("console", "Последние строки консоли"),
            types.BotCommand("cmd", "Выполнить Minecraft-команду"),
            types.BotCommand("players", "Список игроков"),
            types.BotCommand("say", "Сообщение игрокам"),
            types.BotCommand("id", "Показать Telegram ID"),
        ]
    )
    me = bot.get_me()
    logger.info(
        "Bot started as @%s; service=%s admins=%s",
        me.username,
        CFG.service_name,
        sorted(CFG.admin_ids),
    )
    notify_admins_startup()
    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
