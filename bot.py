import os
import docker
from html import escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

# ============================================================
# MANAGED SERVICES
# ============================================================

SERVICES = {
    "torrent": {
        "name": "🎬 Torrent Bot",
        "containers": [
            "torrent-qbittorrent",
            "telegram-torrent-bot",
        ],
        "log_container": "telegram-torrent-bot",
    },
}

# ============================================================
# DOCKER
# ============================================================

docker_client = docker.from_env()


def authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def get_container(name):
    return docker_client.containers.get(name)


def container_status(name):
    try:
        container = get_container(name)
        container.reload()
        return container.status
    except Exception:
        return "not found"


def service_status(service_key):
    service = SERVICES[service_key]

    statuses = [
        container_status(name)
        for name in service["containers"]
    ]

    running = statuses.count("running")

    if running == len(statuses):
        return "🟢 Running"

    if running == 0:
        return "⚫ Stopped"

    return "🟡 Partially Running"


def start_service(service_key):
    service = SERVICES[service_key]

    results = []

    for name in service["containers"]:
        try:
            container = get_container(name)
            container.reload()

            if container.status != "running":
                container.start()
                results.append(f"✅ {name} started")
            else:
                results.append(f"🟢 {name} already running")

        except Exception as error:
            results.append(
                f"❌ {name}: {error}"
            )

    return results


def stop_service(service_key):
    service = SERVICES[service_key]

    results = []

    for name in service["containers"]:
        try:
            container = get_container(name)
            container.reload()

            if container.status == "running":
                container.stop(timeout=10)
                results.append(f"🛑 {name} stopped")
            else:
                results.append(f"⚫ {name} already stopped")

        except Exception as error:
            results.append(
                f"❌ {name}: {error}"
            )

    return results


def restart_service(service_key):
    service = SERVICES[service_key]

    results = []

    for name in service["containers"]:
        try:
            container = get_container(name)
            container.restart(timeout=10)
            results.append(f"🔄 {name} restarted")

        except Exception as error:
            results.append(
                f"❌ {name}: {error}"
            )

    return results


def get_logs(service_key):
    service = SERVICES[service_key]
    name = service["log_container"]

    try:
        container = get_container(name)

        logs = container.logs(
            tail=40,
            timestamps=True,
        ).decode(
            "utf-8",
            errors="replace",
        )

        return logs or "No logs available."

    except Exception as error:
        return f"Error: {error}"


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    buttons = []

    for key, service in SERVICES.items():
        buttons.append([
            InlineKeyboardButton(
                service["name"],
                callback_data=f"service:{key}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "📊 All Status",
            callback_data="all_status",
        ),
        InlineKeyboardButton(
            "🐳 Containers",
            callback_data="containers",
        ),
    ])

    return InlineKeyboardMarkup(buttons)


def service_keyboard(service_key):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "▶️ START",
                callback_data=f"start:{service_key}",
            ),
            InlineKeyboardButton(
                "⏹️ STOP",
                callback_data=f"stop:{service_key}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 RESTART",
                callback_data=f"restart:{service_key}",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 STATUS",
                callback_data=f"status:{service_key}",
            ),
            InlineKeyboardButton(
                "📋 LOGS",
                callback_data=f"logs:{service_key}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ BACK",
                callback_data="home",
            ),
        ],
    ])


# ============================================================
# TEXT
# ============================================================

def home_text():
    return (
        "🐳 <b>Docker Controller</b>\n\n"
        "Select a bot or service to manage."
    )


def service_text(service_key):
    service = SERVICES[service_key]

    lines = [
        f"<b>{escape(service['name'])}</b>",
        "",
        f"Status: {service_status(service_key)}",
        "",
    ]

    for name in service["containers"]:
        status = container_status(name)

        if status == "running":
            icon = "🟢"
        elif status == "exited":
            icon = "⚫"
        else:
            icon = "🟡"

        lines.append(
            f"{icon} {escape(name)} — {status}"
        )

    lines.append("")
    lines.append("Choose an action:")

    return "\n".join(lines)


# ============================================================
# COMMANDS
# ============================================================

async def start_command(update, context):
    if not authorized(update):
        return

    await update.message.reply_text(
        home_text(),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def status_command(update, context):
    if not authorized(update):
        return

    lines = [
        "📊 <b>Docker Services</b>",
        "",
    ]

    for key, service in SERVICES.items():
        lines.append(
            f"{escape(service['name'])}: "
            f"{service_status(key)}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(update, context):
    query = update.callback_query

    if not authorized(update):
        await query.answer(
            "Unauthorized",
            show_alert=True,
        )
        return

    await query.answer()

    data = query.data

    # HOME
    if data == "home":
        await query.edit_message_text(
            home_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    # SERVICE
    if data.startswith("service:"):
        service_key = data.split(":", 1)[1]

        if service_key not in SERVICES:
            return

        await query.edit_message_text(
            service_text(service_key),
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    # ALL STATUS
    if data == "all_status":
        lines = [
            "📊 <b>All Services</b>",
            "",
        ]

        for key, service in SERVICES.items():
            lines.append(
                f"{escape(service['name'])}: "
                f"{service_status(key)}"
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="all_status",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data="home",
                    )
                ],
            ]),
        )
        return

    # CONTAINERS
    if data == "containers":
        try:
            containers = docker_client.containers.list(
                all=True
            )

            lines = [
                "🐳 <b>Docker Containers</b>",
                "",
            ]

            for container in containers:
                status = container.status

                if status == "running":
                    icon = "🟢"
                elif status == "exited":
                    icon = "⚫"
                else:
                    icon = "🟡"

                lines.append(
                    f"{icon} "
                    f"<code>{escape(container.name)}</code>"
                    f" — {escape(status)}"
                )

            if not containers:
                lines.append(
                    "No containers found."
                )

            text = "\n".join(lines)

        except Exception as error:
            text = (
                "❌ <b>Docker Error</b>\n\n"
                f"<pre>{escape(str(error))}</pre>"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="containers",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data="home",
                    )
                ],
            ]),
        )
        return

    # ACTION
    if ":" not in data:
        return

    action, service_key = data.split(":", 1)

    if service_key not in SERVICES:
        return

    service = SERVICES[service_key]

    # STATUS
    if action == "status":
        await query.edit_message_text(
            service_text(service_key),
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    # START
    if action == "start":
        await query.edit_message_text(
            f"🚀 Starting "
            f"<b>{escape(service['name'])}</b>...",
            parse_mode="HTML",
        )

        results = start_service(service_key)

        text = (
            f"🚀 <b>{escape(service['name'])}</b>\n\n"
            + "\n".join(
                escape(x) for x in results
            )
            + "\n\n"
            + f"Status: {service_status(service_key)}"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    # STOP
    if action == "stop":
        await query.edit_message_text(
            f"⏹️ Stopping "
            f"<b>{escape(service['name'])}</b>...",
            parse_mode="HTML",
        )

        results = stop_service(service_key)

        text = (
            f"⏹️ <b>{escape(service['name'])}</b>\n\n"
            + "\n".join(
                escape(x) for x in results
            )
            + "\n\n"
            + f"Status: {service_status(service_key)}"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    # RESTART
    if action == "restart":
        await query.edit_message_text(
            f"🔄 Restarting "
            f"<b>{escape(service['name'])}</b>...",
            parse_mode="HTML",
        )

        results = restart_service(service_key)

        text = (
            f"🔄 <b>{escape(service['name'])}</b>\n\n"
            + "\n".join(
                escape(x) for x in results
            )
            + "\n\n"
            + f"Status: {service_status(service_key)}"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    # LOGS
    if action == "logs":
        logs = get_logs(service_key)

        text = (
            f"📋 <b>{escape(service['name'])} "
            f"Logs</b>\n\n"
            f"<pre>{escape(logs[-3500:])}</pre>"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data=f"logs:{service_key}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data=f"service:{service_key}",
                    )
                ],
            ]),
        )
        return


# ============================================================
# STARTUP
# ============================================================

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand(
            "start",
            "Open Docker controller",
        ),
        BotCommand(
            "status",
            "Show Docker services",
        ),
    ])

    print("✅ Telegram command menu configured.")


def main():
    # Test Docker connection immediately.
    docker_client.ping()

    print("🐳 Docker connection successful.")
    print("🐳 Docker Controller Bot is running...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()
