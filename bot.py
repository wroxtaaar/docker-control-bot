import os
import asyncio
import shlex
import textwrap
import time
import threading
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import docker
import psutil
from html import escape

from telegram.error import BadRequest
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

VPS_BANDWIDTH_GB = float(os.getenv("VPS_BANDWIDTH_GB", "10240") or 10240)
BANDWIDTH_STATE_FILE = Path(os.getenv("BANDWIDTH_STATE_FILE", "/data/bandwidth_state.json"))
PROJECTS_FILE = Path(os.getenv("PROJECTS_FILE", "/data/projects.json"))

docker_client = docker.from_env()

# ============================================================
# MANAGED PROJECTS
# ============================================================

def _load_projects() -> dict:
    try:
        if PROJECTS_FILE.exists():
            with PROJECTS_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
                projects = data.get("projects", data) if isinstance(data, dict) else {}
                return projects if isinstance(projects, dict) else {}
    except Exception:
        pass
    return {}


def _project_compose_files(project: dict) -> tuple[list[Path], str | None]:
    directory = Path(str(project.get("directory") or "")).expanduser().resolve()
    if not directory.is_dir():
        return [], f"Directory does not exist: {directory}"

    configured = project.get("compose")
    if configured:
        candidates = configured if isinstance(configured, list) else [configured]
    else:
        candidates = [
            "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"
        ]

    files = []
    for candidate in candidates:
        compose = Path(str(candidate))
        if not compose.is_absolute():
            compose = directory / compose
        compose = compose.resolve()
        if compose.is_file() and (directory == compose.parent or directory in compose.parents):
            files.append(compose)

    if not files:
        return [], f"No Compose file found in {directory}"
    return files, None


def _run_project(project: dict, args: list[str], timeout: int = 120) -> tuple[int, str]:
    project_type = str(project.get("type", "compose")).lower()

    if project_type == "container":
        container = str(project.get("container") or "").strip()
        if not container:
            return 1, "Project has no container configured."
        action = args[0] if args else ""
        commands = {
            "start": ["docker", "start", container],
            "stop": ["docker", "stop", container],
            "restart": ["docker", "restart", container],
            "ps": ["docker", "inspect", "--format", "{{.Name}}\\t{{.State.Status}}", container],
            "logs": ["docker", "logs", "--tail", "80", container],
        }
        command = commands.get(action)
        if not command:
            return 1, f"Action '{action}' is not supported for this project type."
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
            output = (result.stdout or "") + (result.stderr or "")
            return result.returncode, output.strip() or "Command completed with no output."
        except subprocess.TimeoutExpired:
            return 124, "Command timed out."
        except Exception as error:
            return 1, str(error)

    compose_files, error = _project_compose_files(project)
    if error:
        return 1, error

    directory = compose_files[0].parent
    command = ["docker", "compose"]
    for compose in compose_files:
        command += ["-f", str(compose)]
    command += args

    try:
        result = subprocess.run(
            command,
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip() or "Command completed with no output."
    except subprocess.TimeoutExpired:
        return 124, "Command timed out."
    except FileNotFoundError:
        return 127, "Docker Compose CLI is not available inside the controller container."
    except Exception as error:
        return 1, str(error)


def project_status_text(key: str) -> str:
    projects = _load_projects()
    project = projects.get(key)
    if not project:
        return "❌ Project not found."
    code, output = _run_project(project, ["ps"], timeout=30)
    name = project.get("name", key)
    if code != 0:
        return f"❌ <b>{escape(str(name))}</b>\\n\\n<pre>{escape(output[-3500:])}</pre>"
    return f"📊 <b>{escape(str(name))}</b>\\n\\n<pre>{escape(output[-3500:])}</pre>"


def project_action_text(key: str, action: str) -> str:
    projects = _load_projects()
    project = projects.get(key)
    if not project:
        return "❌ Project not found."

    title = escape(str(project.get("name", key)))
    if action == "stop" and project.get("allow_stop") is False:
        return f"🛡️ <b>{title}</b> is a shared dependency; stopping it is disabled here."
    if action == "restart" and project.get("allow_restart") is False:
        return f"🛡️ <b>{title}</b> is a shared dependency; restarting it is disabled here."

    if action in {"pull", "rebuild"} and str(project.get("type", "compose")).lower() == "container":
        return f"ℹ️ <b>{title}</b> does not support {escape(action)}."

    commands = {
        "start": ["up", "-d"],
        "stop": ["down"],
        "restart": ["restart"],
        "pull": ["pull"],
        "rebuild": ["up", "-d", "--build"],
    }
    args = commands.get(action)
    if str(project.get("type", "compose")).lower() == "container":
        args = [action] if action in {"start", "stop", "restart"} else None
    if not args:
        return "❌ Unsupported project action."

    timeout = 600 if action in {"pull", "rebuild"} else 180
    code, output = _run_project(project, args, timeout=timeout)
    icon = {"start": "🚀", "stop": "⏹️", "restart": "🔄", "pull": "⬇️", "rebuild": "🔨"}[action]
    title = project.get("name", key)
    prefix = f"{icon} <b>{escape(str(title))}</b> — {action.title()}\\n\\n"
    if code != 0:
        prefix = f"❌ <b>{escape(str(title))}</b> — {action.title()} failed\\n\\n"
    return prefix + f"<pre>{escape(output[-3500:])}</pre>"


def project_logs_text(key: str) -> str:
    projects = _load_projects()
    project = projects.get(key)
    if not project:
        return "❌ Project not found."
    code, output = _run_project(project, ["logs"] if str(project.get("type", "compose")).lower() == "compose" else ["logs"], timeout=60)
    title = project.get("name", key)
    return f"📋 <b>{escape(str(title))} Logs</b>\\n\\n<pre>{escape(output[-3500:])}</pre>"


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
        monthly += max(0, tx - previous_tx)

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


def bandwidth_sampler() -> None:
    while True:
        try:
            _bandwidth_snapshot()
        except Exception:
            pass
        time.sleep(60)


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


def host_disk_usage():
    """Read the host filesystem through the existing /home/ubuntu bind mount."""
    path = "/home/ubuntu" if os.path.isdir("/home/ubuntu") else "/"
    return psutil.disk_usage(path), path


def run_host_disk_cleaner_text() -> str:
    """Run the fixed host cleaner in a temporary, isolated helper container.

    The controller already has Docker-socket access. The helper receives a
    read-write bind of the host root only for this fixed cleaner command; it
    has no network and is removed after completion.
    """
    disk_before, _ = host_disk_usage()
    helper = None
    exit_code = 1
    logs = ""

    try:
        controller_name = os.getenv("CONTROLLER_CONTAINER_NAME", "docker-control-bot")
        controller = docker_client.containers.get(controller_name)
        image_id = controller.image.id

        helper = docker_client.containers.run(
            image=image_id,
            command=[
                "/usr/sbin/chroot",
                "/host",
                "/usr/local/sbin/vps-disk-cleaner",
                "--force",
            ],
            volumes={"/": {"bind": "/host", "mode": "rw"}},
            detach=True,
            network_disabled=True,
            pid_mode="host",
            working_dir="/",
            mem_limit="512m",
            labels={"purpose": "vps-disk-cleaner"},
        )

        result = helper.wait(timeout=900)
        exit_code = int(result.get("StatusCode", 1))
        logs = helper.logs(
            stdout=True, stderr=True, tail=100
        ).decode("utf-8", errors="replace")
    except Exception as error:
        logs = (logs + "\n" if logs else "") + f"ERROR: {error}"
        exit_code = 1
    finally:
        if helper is not None:
            try:
                helper.reload()
                if helper.status == "running":
                    helper.stop(timeout=5)
            except Exception:
                pass
            try:
                helper.remove(force=True)
            except Exception:
                pass

    disk_after, _ = host_disk_usage()
    saved = disk_before.used - disk_after.used
    if saved >= 0:
        change_text = f"{format_bytes(saved)} less used"
    else:
        change_text = f"{format_bytes(-saved)} more used during cleanup"

    state = "✅ Completed" if exit_code == 0 else "⚠️ Finished with errors"
    output = (
        f"🧹 <b>Manual VPS Disk Cleanup</b> — {state}\n\n"
        f"<b>Before:</b> {format_bytes(disk_before.used)} used · "
        f"{format_bytes(disk_before.free)} available · "
        f"{format_bytes(disk_before.total)} total ({disk_before.percent:.1f}%)\n"
        f"<b>After:</b> {format_bytes(disk_after.used)} used · "
        f"{format_bytes(disk_after.free)} available · "
        f"{format_bytes(disk_after.total)} total ({disk_after.percent:.1f}%)\n"
        f"<b>Net change:</b> {change_text}\n\n"
        f"<pre>{escape(logs[-2500:] or 'Cleaner returned no output.')}</pre>"
    )
    return output


def vps_stats_text() -> str:
    try:
        cpu = psutil.cpu_percent(interval=0.35)
        cores = psutil.cpu_count() or 1
        load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0, 0, 0)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk, _ = host_disk_usage()
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
            f"{disk_icon} VPS Disk: <b>{format_bytes(disk.used)} used</b> / "
            f"<b>{format_bytes(disk.free)} available</b> of {format_bytes(disk.total)} "
            f"({disk.percent:.1f}%)\n"
            "🌐 <b>Network</b>\n"
            f"⬇️ RX: <b>{format_bytes(bandwidth['rx'])}</b> ({format_rate(bandwidth['rx_rate'])})\n"
            f"⬆️ TX: <b>{format_bytes(bandwidth['tx'])}</b> ({format_rate(bandwidth['tx_rate'])})\n"
            f"📊 Month outbound: <b>{format_bytes(bandwidth['monthly'])}</b>\n"
            + (f"📦 Remaining: <b>{format_bytes(bandwidth['remaining'])}</b> ({bandwidth['usage_pct']:.1f}% used)\n" if bandwidth["allowance"] else "")
            + f"⏱️ Uptime: {format_uptime(time.time() - psutil.boot_time())}\n\n"
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Projects", callback_data="projects")],
        [
            InlineKeyboardButton("🖥️ VPS Stats", callback_data="vps_stats"),
            InlineKeyboardButton("📈 Docker Stats", callback_data="docker_stats"),
        ],
        [InlineKeyboardButton("🌐 Bandwidth", callback_data="bandwidth")],
        [InlineKeyboardButton("🧹 Clean Disk Now", callback_data="disk_clean_confirm")],
    ])


def projects_keyboard():
    projects = _load_projects()
    buttons = [
        [InlineKeyboardButton(str(project.get("name", key)), callback_data=f"project:{key}")]
        for key, project in projects.items()
    ]
    buttons.append([InlineKeyboardButton("⬅️ BACK", callback_data="home")])
    return InlineKeyboardMarkup(buttons)


def project_keyboard(key: str):
    project = _load_projects().get(key, {})
    is_compose = str(project.get("type", "compose")).lower() == "compose"

    controls = []
    if project.get("allow_start", True):
        controls.append(InlineKeyboardButton("▶️ START", callback_data=f"paction:start:{key}"))
    if project.get("allow_stop", True):
        controls.append(InlineKeyboardButton("⏹️ STOP", callback_data=f"paction:stop:{key}"))
    if project.get("allow_restart", True):
        controls.append(InlineKeyboardButton("🔄 RESTART", callback_data=f"paction:restart:{key}"))
    controls.append(InlineKeyboardButton("📊 STATUS", callback_data=f"pstatus:{key}"))

    rows = [
        controls,
        [InlineKeyboardButton("📋 LOGS", callback_data=f"plogs:{key}")],
    ]
    if is_compose:
        rows.append([
            InlineKeyboardButton("⬇️ PULL", callback_data=f"paction:pull:{key}"),
            InlineKeyboardButton("🔨 REBUILD", callback_data=f"paction:rebuild:{key}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ PROJECTS", callback_data="projects")])
    return InlineKeyboardMarkup(rows)


def project_text(key: str):
    projects = _load_projects()
    project = projects.get(key)
    if not project:
        return "❌ Project not found."
    name = project.get("name", key)
    directory = project.get("directory", "unknown")
    project_type = str(project.get("type", "compose")).lower()
    if project_type == "container":
        target = project.get("container", "unknown")
        return (
            f"<b>{escape(str(name))}</b>\\n\\n"
            f"📁 Directory: <code>{escape(str(directory))}</code>\\n"
            f"🐳 Project type: <code>container-backed</code>\\n"
            f"🎯 Project target: <code>{escape(str(target))}</code>\\n\\n"
            "Choose an action:"
        )
    compose = project.get("compose", "auto-detect")
    return (
        f"<b>{escape(str(name))}</b>\\n\\n"
        f"📁 Directory: <code>{escape(str(directory))}</code>\\n"
        f"🐳 Compose: <code>{escape(str(compose))}</code>\\n\\n"
        "Choose an action:"
    )


def project_text(key: str):
    projects = _load_projects()
    project = projects.get(key)
    if not project:
        return "❌ Project not found."
    name = project.get("name", key)
    directory = project.get("directory", "unknown")
    compose = project.get("compose", "auto-detect")
    return (
        f"<b>{escape(str(name))}</b>\\n\\n"
        f"📁 Directory: <code>{escape(str(directory))}</code>\\n"
        f"🐳 Compose: <code>{escape(str(compose))}</code>\\n\\n"
        "Choose an action:"
    )


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
        "<b>Projects</b>\n"
        "/projects — list configured Compose projects\n\n"
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


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    projects = _load_projects()
    if not projects:
        await update.message.reply_text("📦 No projects configured.", parse_mode="HTML")
        return
    lines = ["📦 <b>Projects</b>", ""]
    for key, project in projects.items():
        lines.append(f"• <code>{escape(key)}</code> — {escape(str(project.get('name', key)))}")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=projects_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(help_text(), parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    projects = _load_projects()
    if not projects:
        text = "📦 <b>Projects</b>\\n\\nNo projects configured."
    else:
        lines = ["📊 <b>Project Status</b>", ""]
        for key, project in projects.items():
            code, output = _run_project(project, ["ps"], timeout=30)
            state = "🟢 Running" if code == 0 and output and "running" in output.lower() else "⚫ Stopped"
            if code != 0:
                state = "⚠️ Error"
            lines.append(f"{escape(str(project.get('name', key)))}: {state}")
        text = "\\n".join(lines)

    await update.message.reply_text(
        text,
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
            [InlineKeyboardButton("🧹 Clean Disk Now", callback_data="disk_clean_confirm")],
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
        BotCommand("status", "Show project status"),
        BotCommand("projects", "List managed projects"),
        BotCommand("help", "Show help"),
    ])
    print("✅ Telegram command menu configured.")


# ============================================================
# CALLBACKS
# ============================================================

async def safe_edit_message(query, *args, **kwargs):
    """Edit a callback message without failing when Telegram reports no change."""
    try:
        return await query.edit_message_text(*args, **kwargs)
    except BadRequest as error:
        if "Message is not modified" in str(error):
            return None
        raise


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "home":
        await safe_edit_message(query, 
            home_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if data == "disk_clean_confirm":
        await safe_edit_message(
            query,
            "🧹 <b>Run immediate disk cleanup?</b>\n\n"
            "This will run the VPS cleaner with <code>--force</code>, even below the 40% threshold. "
            "It cleans the APT cache, archived journal logs older than 30 days, and eligible "
            "Docker build cache older than 7 days. It does not remove project files, images, "
            "containers, or volumes.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Run Cleanup Now", callback_data="disk_clean_run")],
                [InlineKeyboardButton("✖️ Cancel", callback_data="home")],
            ]),
        )
        return

    if data == "disk_clean_run":
        await safe_edit_message(
            query,
            "⏳ <b>Running VPS disk cleanup…</b>\nThis may take a few minutes.",
            parse_mode="HTML",
        )
        result = await asyncio.to_thread(run_host_disk_cleaner_text)
        await safe_edit_message(
            query,
            result,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh VPS Stats", callback_data="vps_stats")],
                [InlineKeyboardButton("⬅️ MAIN MENU", callback_data="home")],
            ]),
        )
        return

    if data == "projects":
        await safe_edit_message(
            query,
            "📦 <b>Projects</b>\n\nSelect a Compose project:",
            parse_mode="HTML",
            reply_markup=projects_keyboard(),
        )
        return

    if data.startswith("project:"):
        key = data.split(":", 1)[1]
        if key not in _load_projects():
            return
        await safe_edit_message(query, project_text(key), parse_mode="HTML", reply_markup=project_keyboard(key))
        return

    if data.startswith("pstatus:"):
        key = data.split(":", 1)[1]
        if key not in _load_projects():
            return
        await safe_edit_message(query, project_status_text(key), parse_mode="HTML", reply_markup=project_keyboard(key))
        return

    if data.startswith("plogs:"):
        key = data.split(":", 1)[1]
        if key not in _load_projects():
            return
        await safe_edit_message(
            query, project_logs_text(key), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"plogs:{key}")],
                [InlineKeyboardButton("⬅️ BACK", callback_data=f"project:{key}")],
            ]),
        )
        return

    if data.startswith("paction:"):
        _, action, key = data.split(":", 2)
        if key not in _load_projects():
            return
        await safe_edit_message(query, f"⏳ Running <b>{escape(action)}</b>...", parse_mode="HTML")
        result = project_action_text(key, action)
        await safe_edit_message(query, result, parse_mode="HTML", reply_markup=project_keyboard(key))
        return

    if data == "add_container_help":
        await safe_edit_message(query, 
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
        await safe_edit_message(query, 
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
        await safe_edit_message(query, 
            vps_stats_text(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="vps_stats")],
                [InlineKeyboardButton("📈 Docker Stats", callback_data="docker_stats")],
                [InlineKeyboardButton("🧹 Clean Disk Now", callback_data="disk_clean_confirm")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "docker_stats":
        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="all_status")],
                [InlineKeyboardButton("⬅️ BACK", callback_data="home")],
            ]),
        )
        return

    if data == "containers":
        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
            f"🐳 <b>{escape(service['name'])} Containers</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("cstatus:"):
        name = data.split(":", 1)[1]
        await safe_edit_message(query, 
            container_detail(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("cstart:"):
        name = data.split(":", 1)[1]
        await safe_edit_message(query, 
            start_container(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("cstop:"):
        name = data.split(":", 1)[1]
        await safe_edit_message(query, 
            stop_container(name),
            parse_mode="HTML",
            reply_markup=generic_container_keyboard(name),
        )
        return

    if data.startswith("crestart:"):
        name = data.split(":", 1)[1]
        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
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
        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
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

        await safe_edit_message(query, 
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"logs:{service_key}")],
                [InlineKeyboardButton("⬅️ BACK", callback_data=f"service:{service_key}")],
            ]),
        )


def main():
    docker_client.ping()
    threading.Thread(target=bandwidth_sampler, name="bandwidth-sampler", daemon=True).start()
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
    application.add_handler(CommandHandler("projects", projects_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
