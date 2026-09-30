import os
import shlex
import textwrap
import time
import json
from datetime import datetime, timezone
from pathlib import Path
import docker
import psutil
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
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])
TERABOX_COMPOSE = os.getenv(
    "TERABOX_COMPOSE_FILE",
    "/home/ubuntu/terabox-telegram-bot/vps/docker-compose.yml",
)

VPS_BANDWIDTH_GB = float(os.getenv("VPS_BANDWIDTH_GB", "0") or 0)
BANDWIDTH_STATE_FILE = Path(os.getenv("BANDWIDTH_STATE_FILE", "/data/bandwidth_state.json"))

docker_client = docker.from_env()

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
    "terabox": {
        "name": "📦 TeraBox Bot",
        "containers": [
            "terabox-vps-worker",
        ],
        "log_container": "terabox-vps-worker",
    },
}

# ============================================================
# VPS STATS
# ============================================================

def format_bytes(value: int | float) -> str:
    value = float(value or 0)
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return "0 B"


def format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_rate(bytes_per_second: float) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def _load_bandwidth_state() -> dict:
    try:
        if BANDWIDTH_STATE_FILE.exists():
            with BANDWIDTH_STATE_FILE.open("r", encoding="utf-8") as file:
                state = json.load(file)
                if isinstance(state, dict):
                    return state
    except Exception:
        pass
    return {}


def _save_bandwidth_state(state: dict) -> None:
    try:
        BANDWIDTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = BANDWIDTH_STATE_FILE.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(state, file)
        temp_file.replace(BANDWIDTH_STATE_FILE)
    except Exception:
        pass


def _bandwidth_snapshot() -> dict:
    now = time.time()
    net = psutil.net_io_counters()
    rx, tx = int(net.bytes_recv), int(net.bytes_sent)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    state = _load_bandwidth_state()

    if state.get("month") != month:
        monthly = 0
        previous_rx, previous_tx, previous_time = rx, tx, now
    else:
        previous_rx = int(state.get("last_rx") or rx)
        previous_tx = int(state.get("last_tx") or tx)
        previous_time = float(state.get("last_time") or now)
        monthly = int(state.get("monthly_bytes") or 0)
        monthly += max(0, rx - previous_rx) + max(0, tx - previous_tx)

    _save_bandwidth_state({
        "month": month,
        "monthly_bytes": monthly,
        "last_rx": rx,
        "last_tx": tx,
        "last_time": now,
    })

    elapsed = max(0.1, now - previous_time)
    allowance = VPS_BANDWIDTH_GB * 1024 ** 3 if VPS_BANDWIDTH_GB > 0 else 0
    remaining = max(0, allowance - monthly) if allowance else 0
    usage_pct = monthly / allowance * 100 if allowance else 0

    return {
        "rx": rx,
        "tx": tx,
        "monthly": monthly,
        "rx_rate": max(0, rx - previous_rx) / elapsed,
        "tx_rate": max(0, tx - previous_tx) / elapsed,
        "allowance": allowance,
        "remaining": remaining,
        "usage_pct": usage_pct,
        "month": month,
    }


def bandwidth_text() -> str:
    b = _bandwidth_snapshot()
    month = datetime.strptime(b["month"], "%Y-%m").strftime("%B %Y")
    lines = [
        "🌐 <b>Network & Bandwidth</b>",
        "",
        f"📅 Period: <b>{month}</b>",
        f"⬇️ RX total: <b>{format_bytes(b['rx'])}</b>",
        f"⬆️ TX total: <b>{format_bytes(b['tx'])}</b>",
        f"📊 Interface total: <b>{format_bytes(b['rx'] + b['tx'])}</b>",
        "",
        "⚡ <b>Current speed</b>",
        f"⬇️ RX: <b>{format_rate(b['rx_rate'])}</b>",
        f"⬆️ TX: <b>{format_rate(b['tx_rate'])}</b>",
        "",
        f"📈 <b>This month (observed):</b> {format_bytes(b['monthly'])}",
    ]
    if b["allowance"]:
        icon = "🔴" if b["usage_pct"] >= 90 else "🟡" if b["usage_pct"] >= 75 else "🟢"
        lines += [
            "",
            f"{icon} <b>Allowance:</b> {format_bytes(b['allowance'])}",
            f"📦 <b>Remaining:</b> {format_bytes(b['remaining'])}",
            f"📊 <b>Used:</b> {b['usage_pct']:.1f}%",
        ]
    else:
        lines += ["", "ℹ️ Set <code>VPS_BANDWIDTH_GB</code> to show remaining allowance."]
    return "\n".join(lines)


def vps_stats_text() -> str:
    try:
        cpu = psutil.cpu_percent(interval=0.35)
        cores = psutil.cpu_count() or 1
        load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0, 0, 0)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        bandwidth = _bandwidth_snapshot()
        containers = docker_client.containers.list(all=True)
        running = sum(c.status == "running" for c in containers)

        mem_icon = "🔴" if memory.percent >= 90 else "🟡" if memory.percent >= 75 else "🟢"
        disk_icon = "🔴" if disk.percent >= 90 else "🟡" if disk.percent >= 75 else "🟢"

        return (
            "🖥️ <b>VPS Statistics</b>\n\n"
            f"⚙️ CPU: <b>{cpu:.1f}%</b> ({cores} cores)\n"
            f"📈 Load: <code>{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}</code>\n"
            f"{mem_icon} RAM: <b>{format_bytes(memory.used)}</b> / {format_bytes(memory.total)} ({memory.percent:.1f}%)\n"
            f"💾 Swap: <b>{format_bytes(swap.used)}</b> / {format_bytes(swap.total)} ({swap.percent:.1f}%)\n"
            f"{disk_icon} Disk: <b>{format_bytes(disk.used)}</b> / {format_bytes(disk.total)} ({disk.percent:.1f}%)\n"
            "🌐 <b>Network</b>\n"
            f"⬇️ RX: <b>{format_bytes(bandwidth['rx'])}</b> ({format_rate(bandwidth['rx_rate'])})\n"
            f"⬆️ TX: <b>{format_bytes(bandwidth['tx'])}</b> ({format_rate(bandwidth['tx_rate'])})\n"
            f"📊 Month: <b>{format_bytes(bandwidth['monthly'])}</b> observed\n"
            + (f"📦 Remaining: <b>{format_bytes(bandwidth['remaining'])}</b> ({bandwidth['usage_pct']:.1f}% used)\n" if bandwidth["allowance"] else "")
            f"⏱️ Uptime: {format_uptime(time.time() - psutil.boot_time())}\n\n"
            f"🐳 Docker: <b>{running} running</b> / {len(containers)} total"
        )
    except Exception as error:
        return f"❌ Could not read VPS statistics: <pre>{escape(str(error))}</pre>"


def container_stats_text() -> str:
    lines = ["🐳 <b>Docker Resource Usage</b>", ""]
    containers = sorted(docker_client.containers.list(all=True), key=lambda c: c.name.lower())

    for container in containers:
        if container.status != "running":
            lines.append(f"⚫ <code>{escape(container.name)}</code> — stopped")
            continue
        try:
            stats = container.stats(stream=False)
            memory = stats.get("memory_stats", {})
            usage = int(memory.get("usage") or 0)
            limit = int(memory.get("limit") or 0)
            mem_pct = usage * 100 / limit if limit else 0

            cpu_stats = stats.get("cpu_stats", {})
            prev = stats.get("precpu_stats", {})
            cpu_delta = (
                (cpu_stats.get("cpu_usage", {}).get("total_usage") or 0)
                - (prev.get("cpu_usage", {}).get("total_usage") or 0)
            )
            system_delta = (
                (cpu_stats.get("system_cpu_usage") or 0)
                - (prev.get("system_cpu_usage") or 0)
            )
            online = cpu_stats.get("online_cpus") or psutil.cpu_count() or 1
            cpu_pct = cpu_delta / system_delta * online * 100 if system_delta > 0 else 0

            limit_text = f" / {format_bytes(limit)} ({mem_pct:.1f}%)" if limit else ""
            lines.append(
                f"🟢 <code>{escape(container.name)}</code> — "
                f"CPU {cpu_pct:.1f}% | RAM {format_bytes(usage)}{limit_text}"
            )
        except Exception:
            lines.append(f"🟡 <code>{escape(container.name)}</code> — stats unavailable")

    return "\n".join(lines)


# ============================================================
# DOCKER
# ============================================================

def authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def get_container(name: str):
    return docker_client.containers.get(name)


def container_status(name: str) -> str:
    try:
        container = get_container(name)
        container.reload()
        return container.status
    except docker.errors.NotFound:
        return "not found"
    except Exception:
        return "error"


def service_status(service_key: str) -> str:
    service = SERVICES[service_key]
    statuses = [container_status(name) for name in service["containers"]]
    running = statuses.count("running")

    if running == len(statuses):
        return "🟢 Running"
    if running == 0:
        return "⚫ Stopped"
    return "🟡 Partially Running"


def start_service(service_key: str):
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
            results.append(f"❌ {name}: {error}")

    return results


def stop_service(service_key: str):
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
                results.append(f"⚫ {name} already {container.status}")
        except Exception as error:
            results.append(f"❌ {name}: {error}")

    return results


def restart_service(service_key: str):
    service = SERVICES[service_key]
    results = []

    for name in service["containers"]:
        try:
            container = get_container(name)
            container.restart(timeout=10)
            results.append(f"🔄 {name} restarted")
        except Exception as error:
            results.append(f"❌ {name}: {error}")

    return results


def get_logs(service_key: str):
    service = SERVICES[service_key]
    name = service["log_container"]

    try:
        container = get_container(name)
        logs = container.logs(tail=60, timestamps=True).decode(
            "utf-8", errors="replace"
        )
        return logs or "No logs available."
    except Exception as error:
        return f"Error: {error}"


def all_containers_text() -> str:
    containers = docker_client.containers.list(all=True)
    lines = ["🐳 <b>Docker Containers</b>", ""]

    for container in sorted(containers, key=lambda item: item.name.lower()):
        status = container.status
        icon = (
            "🟢" if status == "running"
            else "⚫" if status == "exited"
            else "🟡"
        )
        lines.append(
            f"{icon} <code>{escape(container.name)}</code> — {escape(status)}"
        )

    if len(lines) == 2:
        lines.append("No containers found.")

    return "\n".join(lines)


# ============================================================
# GENERIC CONTAINER CONTROLS
# ============================================================

def start_container(name: str) -> str:
    try:
        container = get_container(name)
        container.reload()
        if container.status == "running":
            return f"🟢 <code>{escape(name)}</code> is already running."
        container.start()
        return f"✅ Started <code>{escape(name)}</code>."
    except Exception as error:
        return f"❌ Failed to start <code>{escape(name)}</code>: {escape(str(error))}"


def stop_container(name: str) -> str:
    try:
        container = get_container(name)
        container.reload()
        if container.status != "running":
            return f"⚫ <code>{escape(name)}</code> is already {escape(container.status)}."
        container.stop(timeout=10)
        return f"🛑 Stopped <code>{escape(name)}</code>."
    except Exception as error:
        return f"❌ Failed to stop <code>{escape(name)}</code>: {escape(str(error))}"


def restart_container(name: str) -> str:
    try:
        container = get_container(name)
        container.restart(timeout=10)
        return f"🔄 Restarted <code>{escape(name)}</code>."
    except Exception as error:
        return f"❌ Failed to restart <code>{escape(name)}</code>: {escape(str(error))}"


def container_detail(name: str) -> str:
    try:
        container = get_container(name)
        container.reload()
        attrs = container.attrs

        state = attrs.get("State", {})
        config = attrs.get("Config", {})
        host_config = attrs.get("HostConfig", {})

        restart_policy = host_config.get("RestartPolicy", {}) or {}
        restart_name = restart_policy.get("Name") or "no"

        image_name = config.get("Image") or str(container.image.tags[:1] or "unknown")

        lines = [
            f"🐳 <b>{escape(name)}</b>",
            "",
            f"Status: <code>{escape(container.status)}</code>",
            f"Image: <code>{escape(image_name)}</code>",
            f"Restart: <code>{escape(restart_name)}</code>",
            f"Started: <code>{escape(str(state.get('StartedAt') or 'unknown'))}</code>",
            f"Finished: <code>{escape(str(state.get('FinishedAt') or 'unknown'))}</code>",
        ]
        return "\n".join(lines)
    except Exception as error:
        return f"❌ Docker error: <pre>{escape(str(error))}</pre>"


# ============================================================
# DYNAMIC CONTAINER CREATION
# ============================================================

def create_container_from_image(image: str, name: str, ports: dict[str, int] | None = None, env: dict[str, str] | None = None) -> str:
    safe_name = name.strip()
    if not safe_name or len(safe_name) > 63:
        return "❌ Container name must be 1–63 characters."

    if not all(ch.isalnum() or ch in "._-" for ch in safe_name):
        return "❌ Container name may contain only letters, numbers, '.', '_' and '-'."

    image = image.strip()
    if not image:
        return "❌ Image name is required."

    try:
        docker_client.images.get(image)
    except docker.errors.ImageNotFound:
        try:
            docker_client.images.pull(image)
        except Exception as error:
            return f"❌ Could not pull image <code>{escape(image)}</code>: <pre>{escape(str(error))}</pre>"
    except Exception as error:
        return f"❌ Could not inspect image: <pre>{escape(str(error))}</pre>"

    try:
        existing = get_container(safe_name)
        existing.reload()
        return (
            f"❌ Container <code>{escape(safe_name)}</code> already exists "
            f"({escape(existing.status)})."
        )
    except docker.errors.NotFound:
        pass

    try:
        normalized_ports = {}
        for container_port, host_port in (ports or {}).items():
            normalized_ports[str(container_port)] = int(host_port)

        container = docker_client.containers.create(
            image=image,
            name=safe_name,
            environment=env or {},
            ports=normalized_ports or None,
            restart_policy={"Name": "unless-stopped"},
            labels={
                "managed-by": "docker-control-bot",
            },
        )
        container.start()
        return (
            f"✅ <b>Container created</b>\n\n"
            f"Name: <code>{escape(safe_name)}</code>\n"
            f"Image: <code>{escape(image)}</code>\n"
            f"Status: <code>{escape(container.status)}</code>\n"
            "Restart policy: <code>unless-stopped</code>"
        )
    except Exception as error:
        return f"❌ Failed to create container: <pre>{escape(str(error))}</pre>"


def parse_create_args(args: list[str]):
    if len(args) < 2:
        return None, None, {}, {}, (
            "Usage:\n"
            "<code>/container_create NAME IMAGE [PORT=HOST_PORT] [ENV=KEY=VALUE]</code>\n\n"
            "Example:\n"
            "<code>/container_create nginx nginx:alpine 8080=80</code>"
        )

    name = args[0]
    image = args[1]
    ports = {}
    env = {}

    for token in args[2:]:
        if token.startswith("ENV="):
            value = token[4:]
            if "=" not in value:
                return None, None, {}, {}, "❌ ENV must use <code>KEY=VALUE</code>."
            key, val = value.split("=", 1)
            env[key] = val
            continue

        if "=" in token:
            container_port, host_port = token.split("=", 1)
            if not container_port.isdigit() or not host_port.isdigit():
                return None, None, {}, {}, "❌ Port mapping must look like <code>8080=80</code>."
            ports[f"{container_port}/tcp"] = int(host_port)
            continue

        return None, None, {}, {}, f"❌ Unrecognized option: <code>{escape(token)}</code>."

    return name, image, ports, env, None


# ============================================================
# UI
# ============================================================

def main_keyboard():
    buttons = [
        [
            InlineKeyboardButton(
                service["name"],
                callback_data=f"service:{key}",
            )
        ]
        for key, service in SERVICES.items()
    ]

    buttons.append([
        InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats"),
        InlineKeyboardButton("🐳 Containers", callback_data="containers"),
    ])
    buttons.append([
        InlineKeyboardButton("📈 Docker Stats", callback_data="docker_stats"),
        InlineKeyboardButton("📊 All Status", callback_data="all_status"),
    ])
    buttons.append([
        InlineKeyboardButton("🌐 Bandwidth", callback_data="bandwidth"),
    ])
    buttons.append([
        InlineKeyboardButton("➕ Add Container", callback_data="add_container_help"),
    ])

    return InlineKeyboardMarkup(buttons)


def service_keyboard(service_key: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ START", callback_data=f"start:{service_key}"),
            InlineKeyboardButton("⏹️ STOP", callback_data=f"stop:{service_key}"),
        ],
        [
            InlineKeyboardButton("🔄 RESTART", callback_data=f"restart:{service_key}"),
        ],
        [
            InlineKeyboardButton("📊 STATUS", callback_data=f"status:{service_key}"),
            InlineKeyboardButton("📋 LOGS", callback_data=f"logs:{service_key}"),
        ],
        [
            InlineKeyboardButton("🐳 CONTAINER", callback_data=f"container_list:{service_key}"),
        ],
        [
            InlineKeyboardButton("⬅️ BACK", callback_data="home"),
        ],
    ])


def generic_container_keyboard(name: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ START", callback_data=f"cstart:{name}"),
            InlineKeyboardButton("⏹️ STOP", callback_data=f"cstop:{name}"),
        ],
        [
            InlineKeyboardButton("🔄 RESTART", callback_data=f"crestart:{name}"),
        ],
        [
            InlineKeyboardButton("📊 STATUS", callback_data=f"cstatus:{name}"),
            InlineKeyboardButton("📋 LOGS", callback_data=f"clogs:{name}"),
        ],
        [
            InlineKeyboardButton("⬅️ BACK", callback_data="containers"),
        ],
    ])


def home_text():
    return (
        "🐳 <b>Docker Controller</b>\n\n"
        "Manage configured services or any Docker container on this VPS.\n\n"
        "Use <code>/help</code> to see the commands."
    )


def service_text(service_key: str):
    service = SERVICES[service_key]
    lines = [
        f"<b>{escape(service['name'])}</b>",
        "",
        f"Status: {service_status(service_key)}",
        "",
    ]

    for name in service["containers"]:
        status = container_status(name)
        icon = "🟢" if status == "running" else "⚫" if status == "exited" else "🟡"
        lines.append(f"{icon} {escape(name)} — {escape(status)}")

    lines += ["", "Choose an action:"]
    return "\n".join(lines)


def help_text():
    return (
        "🐳 <b>Docker Controller Help</b>\n\n"
        "<b>VPS monitoring</b>\n"
        "/stats — CPU, RAM, swap, disk, network and uptime\n"
        "/bandwidth — monthly bandwidth and live network speed\n"
        "/docker_stats — per-container CPU and RAM usage\n\n"
        "<b>Services</b>\n"
        "/start — open controller\n"
        "/status — show services\n"
        "/containers — list all containers\n"
        "/help — show help\n\n"
        "<b>Direct container control</b>\n"
        "<code>/container NAME</code> — open a container\n"
        "<code>/container_start NAME</code>\n"
        "<code>/container_stop NAME</code>\n"
        "<code>/container_restart NAME</code>\n"
        "<code>/container_logs NAME</code>\n\n"
        "<b>Create a container</b>\n"
        "<code>/container_create NAME IMAGE [PORT=HOST_PORT] [ENV=KEY=VALUE]</code>\n\n"
        "Example:\n"
        "<code>/container_create nginx nginx:alpine 8080=80</code>\n\n"
        "The bot pulls the image when needed, creates the container, "
        "applies an <code>unless-stopped</code> restart policy, and starts it."
    )


# ============================================================
# COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        home_text(),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(help_text(), parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    lines = ["📊 <b>Docker Services</b>", ""]
    for key, service in SERVICES.items():
        lines.append(
            f"{escape(service['name'])}: {service_status(key)}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        vps_stats_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="vps_stats")],
            [InlineKeyboardButton("📈 Docker Stats", callback_data="docker_stats")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
        ]),
    )


async def bandwidth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        bandwidth_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="bandwidth")],
            [InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
        ]),
    )


async def docker_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        container_stats_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="docker_stats")],
            [InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
        ]),
    )


async def containers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    await update.message.reply_text(
        all_containers_text(),
        parse_mode="HTML",
    )


async def container_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/container NAME</code>",
            parse_mode="HTML",
        )
        return

    name = context.args[0]
    await update.message.reply_text(
        container_detail(name),
        parse_mode="HTML",
        reply_markup=generic_container_keyboard(name),
    )


async def container_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/container_start NAME</code>", parse_mode="HTML")
        return
    await update.message.reply_text(start_container(context.args[0]), parse_mode="HTML")


async def container_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/container_stop NAME</code>", parse_mode="HTML")
        return
    await update.message.reply_text(stop_container(context.args[0]), parse_mode="HTML")


async def container_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/container_restart NAME</code>", parse_mode="HTML")
        return
    await update.message.reply_text(restart_container(context.args[0]), parse_mode="HTML")


async def container_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/container_logs NAME</code>", parse_mode="HTML")
        return

    name = context.args[0]
    try:
        container = get_container(name)
        logs = container.logs(tail=80, timestamps=True).decode("utf-8", errors="replace")
        if not logs:
            logs = "No logs available."
    except Exception as error:
        logs = f"Error: {error}"

    # Telegram message limit safety.
    await update.message.reply_text(
        f"📋 <b>{escape(name)} Logs</b>\n\n<pre>{escape(logs[-3800:])}</pre>",
        parse_mode="HTML",
    )


async def container_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    name, image, ports, env, error = parse_create_args(context.args)
    if error:
        await update.message.reply_text(error, parse_mode="HTML")
        return

    await update.message.reply_text(
        f"⏳ Pulling/creating <code>{escape(name)}</code> from "
        f"<code>{escape(image)}</code>...",
        parse_mode="HTML",
    )
    result = create_container_from_image(image, name, ports, env)
    await update.message.reply_text(result, parse_mode="HTML")


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Open Docker controller"),
        BotCommand("stats", "Show VPS CPU, RAM and disk"),
        BotCommand("bandwidth", "Show bandwidth usage and speed"),
        BotCommand("docker_stats", "Show container CPU and RAM"),
        BotCommand("status", "Show Docker services"),
        BotCommand("containers", "List all Docker containers"),
        BotCommand("container", "Open one container"),
        BotCommand("container_start", "Start a container"),
        BotCommand("container_stop", "Stop a container"),
        BotCommand("container_restart", "Restart a container"),
        BotCommand("container_logs", "Show container logs"),
        BotCommand("container_create", "Create a Docker container"),
        BotCommand("help", "Show help"),
    ])
    print("✅ Telegram command menu configured.")


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "home":
        await query.edit_message_text(
            home_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if data == "add_container_help":
        await query.edit_message_text(
            "➕ <b>Add Docker Container</b>\n\n"
            "Use:\n"
            "<code>/container_create NAME IMAGE [PORT=HOST_PORT] [ENV=KEY=VALUE]</code>\n\n"
            "Example:\n"
            "<code>/container_create nginx nginx:alpine 8080=80</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")]
            ]),
        )
        return

    if data == "bandwidth":
        await query.edit_message_text(
            bandwidth_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="bandwidth")],
                [InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "vps_stats":
        await query.edit_message_text(
            vps_stats_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="vps_stats")],
                [InlineKeyboardButton("📈 Docker Stats", callback_data="docker_stats")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "docker_stats":
        await query.edit_message_text(
            container_stats_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="docker_stats")],
                [InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "all_status":
        lines = ["📊 <b>All Services</b>", ""]
        for key, service in SERVICES.items():
            lines.append(
                f"{escape(service['name'])}: {service_status(key)}"
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="all_status")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "containers":
        await query.edit_message_text(
            all_containers_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="containers")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

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

    if data.startswith("container_list:"):
        service_key = data.split(":", 1)[1]
        if service_key not in SERVICES:
            return

        service = SERVICES[service_key]
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"cstatus:{name}")]
            for name in service["containers"]
        ]
        buttons.append([InlineKeyboardButton("⬅️ BACK", callback_data=f"service:{service_key}")])

        await query.edit_message_text(
            f"🐳 <b>{escape(service['name'])} Containers</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("cstatus:"):
        name = data.split(":", 1)[1]
        await query.edit_message_text(
            container_detail(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("cstart:"):
        name = data.split(":", 1)[1]
        await query.edit_message_text(
            start_container(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("cstop:"):
        name = data.split(":", 1)[1]
        await query.edit_message_text(
            stop_container(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("crestart:"):
        name = data.split(":", 1)[1]
        await query.edit_message_text(
            restart_container(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("clogs:"):
        name = data.split(":", 1)[1]
        try:
            container = get_container(name)
            logs = container.logs(tail=50, timestamps=True).decode("utf-8", errors="replace")
            logs = logs or "No logs available."
        except Exception as error:
            logs = f"Error: {error}"

        await query.edit_message_text(
            f"📋 <b>{escape(name)} Logs</b>\n\n<pre>{escape(logs[-3500:])}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"clogs:{name}")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="containers")],
            ]),
        )
        return

    if ":" not in data:
        return

    action, service_key = data.split(":", 1)
    if service_key not in SERVICES:
        return

    service = SERVICES[service_key]

    if action == "status":
        await query.edit_message_text(
            service_text(service_key),
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    if action in {"start", "stop", "restart"}:
        action_label = {
            "start": "🚀 Starting",
            "stop": "⏹️ Stopping",
            "restart": "🔄 Restarting",
        }[action]

        await query.edit_message_text(
            f"{action_label} <b>{escape(service['name'])}</b>...",
            parse_mode="HTML",
        )

        if action == "start":
            results = start_service(service_key)
        elif action == "stop":
            results = stop_service(service_key)
        else:
            results = restart_service(service_key)

        text = (
            f"{action_label} <b>{escape(service['name'])}</b>\n\n"
            + "\n".join(escape(x) for x in results)
            + "\n\n"
            + f"Status: {service_status(service_key)}"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=service_keyboard(service_key),
        )
        return

    if action == "logs":
        logs = get_logs(service_key)
        text = (
            f"📋 <b>{escape(service['name'])} Logs</b>\n\n"
            f"<pre>{escape(logs[-3500:])}</pre>"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"logs:{service_key}")],
                [InlineKeyboardButton("⬅️ BACK", callback_data=f"service:{service_key}")],
            ]),
        )


def main():
    docker_client.ping()
    print("🐳 Docker connection successful.")
    print("🐳 Docker Controller Bot is running...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("bandwidth", bandwidth_command))
    application.add_handler(CommandHandler("docker_stats", docker_stats_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("containers", containers_command))
    application.add_handler(CommandHandler("container", container_command))
    application.add_handler(CommandHandler("container_start", container_start_command))
    application.add_handler(CommandHandler("container_stop", container_stop_command))
    application.add_handler(CommandHandler("container_restart", container_restart_command))
    application.add_handler(CommandHandler("container_logs", container_logs_command))
    application.add_handler(CommandHandler("container_create", container_create_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
