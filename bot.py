import os
import subprocess
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

PROJECT_DIR = os.path.expanduser("~/telegram-torrent-bot")


def authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def docker_command(command):
    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = (result.stdout + result.stderr).strip()

    if not output:
        output = "Command completed."

    return output


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    await update.message.reply_text(
        "🐳 Docker Controller\n\n"
        "/status - Docker status\n"
        "/containers - List containers\n"
        "/up - Start torrent services\n"
        "/down - Stop torrent services\n"
        "/restart - Restart torrent services\n"
        "/logs - Show recent bot logs"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    output = docker_command([
        "docker",
        "compose",
        "ps",
    ])

    await update.message.reply_text(
        f"🐳 Docker Status\n\n```text\n{output[:3500]}\n```",
        parse_mode="Markdown",
    )


async def containers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    output = docker_command([
        "docker",
        "ps",
        "--format",
        "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
    ])

    await update.message.reply_text(
        f"📦 Containers\n\n```text\n{output[:3500]}\n```",
        parse_mode="Markdown",
    )


async def up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    await update.message.reply_text("🚀 Starting torrent services...")

    output = docker_command([
        "docker",
        "compose",
        "up",
        "-d",
        "qbittorrent",
        "bot",
    ])

    await update.message.reply_text(
        f"✅ Services started.\n\n```text\n{output[:3000]}\n```",
        parse_mode="Markdown",
    )


async def down(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    await update.message.reply_text("🛑 Stopping torrent services...")

    output = docker_command([
        "docker",
        "compose",
        "stop",
        "qbittorrent",
        "bot",
    ])

    await update.message.reply_text(
        f"✅ Services stopped.\n\n```text\n{output[:3000]}\n```",
        parse_mode="Markdown",
    )


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    await update.message.reply_text("🔄 Restarting torrent services...")

    output = docker_command([
        "docker",
        "compose",
        "restart",
        "qbittorrent",
        "bot",
    ])

    await update.message.reply_text(
        f"✅ Services restarted.\n\n```text\n{output[:3000]}\n```",
        parse_mode="Markdown",
    )


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    output = docker_command([
        "docker",
        "compose",
        "logs",
        "--tail=40",
        "bot",
    ])

    await update.message.reply_text(
        f"📋 Torrent Bot Logs\n\n```text\n{output[-3500:]}\n```",
        parse_mode="Markdown",
    )


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("containers", containers))
    application.add_handler(CommandHandler("up", up))
    application.add_handler(CommandHandler("down", down))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("logs", logs))

    print("🐳 Docker Controller Bot is running...")

    application.run_polling()


if __name__ == "__main__":
    main()
