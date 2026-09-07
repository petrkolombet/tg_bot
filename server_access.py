"""Server access module — restricted shell execution.

Workspace (WORKSPACE) — полный доступ (read/write/exec).
Вне workspace — только read-only + мониторинг.
"""

import os
import shlex
import subprocess
import asyncio
import logging
import re
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import config
logger = logging.getLogger(__name__)

WORKSPACE = config.BASE_DIR / "workspace"
LOG_FILE = str(config.BASE_DIR / "server_access.log")

# Мониторинг — разрешённые команды (read-only, безопасные)
MONITORING_CMDS = {
    # Система
    "ps", "top", "htop", "uptime", "w", "who", "id", "uname", "hostname",
    "date", "timedatectl",
    # Память
    "free", "vmstat", "slabtop",
    # Диск
    "df", "du", "lsblk", "mount", "findmnt",
    # Сеть
    "ss", "netstat", "ip", "ifconfig", "ping", "traceroute", "mtr",
    "dig", "nslookup", "host", "curl", "wget",
    # Процессы
    "pgrep", "pidof", "pstree",
    # Логи/чтение
    "cat", "head", "tail", "less", "more", "grep", "awk", "sed",
    "wc", "sort", "uniq", "cut", "tr", "tee", "xargs", "jq",
    "file", "stat", "ls", "tree", "realpath", "readlink", "basename", "dirname",
    "env", "printenv", "which", "whereis", "type",
    "journalctl", "systemctl status", "systemctl list",
    "cat /proc/cpuinfo", "cat /proc/meminfo", "cat /proc/loadavg",
    "cat /proc/version", "cat /proc/uptime",
    "dmesg", "lscpu", "lsmem", "lsusb", "lspci",
    # Python (безопасные)
    "python3 -c", "python3 -m", "python3 --version",
}

# Команды с аргументами (prefix match)
MONITORING_PREFIXES = [
    "systemctl status", "systemctl list", "systemctl show",
    "journalctl", "ss ", "netstat ", "ip ", "ping ",
    "cat /proc/", "ls ", "ls -", "grep ", "awk ",
    "find ", "du ", "df ", "free", "ps ", "ps -",
    "curl ", "wget ", "dig ", "nslookup ",
    "python3 -c ", "python3 -m ",
]

# Запрещённые паттерны (даже в workspace)
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",           # rm -rf /
    r"mkfs\.",                  # форматирование
    r"dd\s+if=",               # dd
    r"chmod\s+777",            # chmod 777
    r">\s*/dev/sd",            # запись в блочные устройства
    r"shutdown", r"reboot",    # выключение
    r"init\s+[06]",            # остановка системы
    r"kill\s+-9\s+1",          # kill init
    r"wget.*\|\s*sh",          # pipe wget to sh
    r"curl.*\|\s*sh",          # pipe curl to sh
]

MSK = timezone(timedelta(hours=3))


def _is_safe_command(cmd: str) -> tuple[bool, str]:
    """Проверяет, безопасна ли команда."""
    cmd_stripped = cmd.strip()
    if not cmd_stripped:
        return False, "пустая команда"

    # Проверка опасных паттернов
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_stripped, re.IGNORECASE):
            return False, f"опасный паттерн: {pattern}"

    return True, ""


def _is_monitoring(cmd: str) -> bool:
    """Проверяет, является ли команда мониторингом."""
    cmd_stripped = cmd.strip()
    base = shlex.split(cmd_stripped)[0] if cmd_stripped else ""

    # Точное совпадение
    if base in MONITORING_CMDS:
        return True

    # Prefix match
    for prefix in MONITORING_PREFIXES:
        if cmd_stripped.startswith(prefix):
            return True

    return False


def _is_write_operation(cmd: str, target_path: str = None) -> bool:
    """Проверяет, является ли команда операцией записи."""
    cmd_stripped = cmd.strip()

    # Определяем write-команды
    write_bases = {"cp", "mv", "mkdir", "touch", "chmod", "chown", "chgrp",
                   "ln", "tee", "dd", "install", "tar", "zip", "unzip",
                   "python3", "node", "pip", "npm", "apt", "git"}
    base = shlex.split(cmd_stripped)[0] if cmd_stripped else ""

    if base in write_bases:
        return True

    # Перенаправление вывода
    if re.search(r">\s*\S", cmd_stripped) or re.search(r">>\s*\S", cmd_stripped):
        return True

    # Pipes с tee
    if "tee" in cmd_stripped:
        return True

    return False


def _is_inside_workspace(path: str) -> bool:
    """Проверяет, находится ли путь внутри workspace."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = WORKSPACE / p
        real_path = p.resolve()
        real_workspace = WORKSPACE.resolve()
        return str(real_path).startswith(str(real_workspace))
    except Exception:
        return False


def check_permission(cmd: str) -> tuple[bool, str, str]:
    """
    Проверяет разрешение на выполнение команды.
    Возвращает (allowed, mode, reason).
    mode: "write", "read", "monitor"
    """
    safe, reason = _is_safe_command(cmd)
    if not safe:
        return False, "denied", reason

    # Мониторинг — всегда разрешён
    if _is_monitoring(cmd):
        return True, "monitor", "monitoring"

    # Write-операция — только в workspace
    if _is_write_operation(cmd):
        # Извлекаем целевой путь из команды
        parts = shlex.split(cmd)
        target = parts[-1] if len(parts) > 1 else ""
        if target.startswith("-"):
            target = parts[-2] if len(parts) > 2 else ""

        if target and _is_inside_workspace(target):
            return True, "write", f"workspace: {target}"
        else:
            return False, "denied", f"write outside workspace: {target}"

    # Read-only — разрешён везде
    return True, "read", "read-only"


async def execute(cmd: str, timeout: int = 30) -> dict:
    """
    Выполняет команду с проверкой прав.
    Возвращает {"allowed": bool, "mode": str, "output": str, "error": str}
    """
    allowed, mode, reason = check_permission(cmd)

    # Логируем
    ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{ts} | mode={mode} | allowed={allowed} | cmd={cmd[:100]} | reason={reason}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_entry)
    except Exception:
        pass

    if not allowed:
        return {
            "allowed": False,
            "mode": "denied",
            "output": "",
            "error": f"🚫 Запрещено: {reason}"
        }

    sandbox_prefix = _sandbox_prefix("bash", "-c")
    try:
        proc = await asyncio.create_subprocess_exec(
            *sandbox_prefix,
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE,
            env={**os.environ, "LANG": "C", "TERM": "dumb", "HOME": str(WORKSPACE)},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")

        # Ограничиваем вывод
        max_lines = 50
        lines = output.strip().split("\n")
        if len(lines) > max_lines:
            output = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} строк обрезано)"

        return {
            "allowed": True,
            "mode": mode,
            "output": output.strip() or "(пустой вывод)",
            "error": err_output.strip() if err_output else "",
            "returncode": proc.returncode,
        }

    except asyncio.TimeoutError:
        return {
            "allowed": True,
            "mode": mode,
            "output": "",
            "error": f"⏰ Таймаут {timeout} сек"
        }
    except Exception as e:
        return {
            "allowed": True,
            "mode": mode,
            "output": "",
            "error": f"❌ Ошибка: {e}"
        }


def _sandbox_prefix(entry: str, *extra) -> list:
    """Общий bwrap-префикс. entry — исполняемая программа внутри sandbox
    ('bash' — интерактивная команда через -c, 'python3' — тул-скрипт).
    extra — доп. аргументы после entry (например '-c')."""
    return [
        "bwrap", "--unshare-ipc",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/run", "/run",
        "--ro-bind", "/var", "/var",
        "--ro-bind", "/root", "/root",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--setenv", "SYSTEMD_IGNORE_CHROOT", "1",
        "--setenv", "HOME", str(WORKSPACE),
        "--chdir", str(WORKSPACE),
        "--dir", str(WORKSPACE),
        "--bind", str(WORKSPACE), str(WORKSPACE),
        entry, *extra,
    ]


async def execute_custom_tool(script_path: str, method: str, kwargs: dict = None, timeout: int = 60) -> dict:
    """Выполняет кастомный тул (файл из workspace/tools) через универсальный runner.

    script_path — путь к .py тула (обязан лежать в workspace/tools).
    method — имя метода-функции в файле; kwargs — аргументы вызова.
    Модели не нужно писать _main/argparse: runner сам импортирует файл,
    вызывает метод и печатает JSON результата."""
    tool_dir = (WORKSPACE / "tools").resolve()
    script = Path(script_path).resolve()
    # Хард-гарантия: скрипт обязан лежать в workspace/tools
    if not str(script).startswith(str(tool_dir) + os.sep) or script.suffix != ".py":
        return {
            "allowed": False,
            "mode": "denied",
            "output": "",
            "error": f"🚫 Тул вне workspace/tools: {script_path}"
        }

    kwargs = kwargs or {}
    runner = Path(__file__).resolve().parent / "_tool_runner.py"
    cmd = [str(runner), str(script), method]
    stdin_data = json.dumps(kwargs, ensure_ascii=False)

    ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{ts} | mode=tool | allowed=True | cmd={script.name}.{method}({json.dumps(kwargs, ensure_ascii=False)[:80]})\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_entry)
    except Exception:
        pass

    try:
        proc = await asyncio.create_subprocess_exec(
            *_sandbox_prefix("python3"),
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE,
            env={**os.environ, "LANG": "C", "TERM": "dumb", "HOME": str(WORKSPACE)},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8")), timeout=timeout
        )
        output = stdout.decode("utf-8", errors="replace").strip()
        err_output = stderr.decode("utf-8", errors="replace").strip()

        max_lines = 60
        lines = output.split("\n")
        if len(lines) > max_lines:
            output = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} строк обрезано)"

        return {
            "allowed": True,
            "mode": "tool",
            "output": output or "(пустой вывод)",
            "error": err_output,
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        return {
            "allowed": True,
            "mode": "tool",
            "output": "",
            "error": f"⏰ Таймаут {timeout} сек"
        }
    except Exception as e:
        return {
            "allowed": True,
            "mode": "tool",
            "output": "",
            "error": f"❌ Ошибка: {e}"
        }


def get_workspace() -> str:
    """Возвращает путь workspace."""
    return str(WORKSPACE)


def ensure_workspace():
    """Создаёт workspace если не существует."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "tmp").mkdir(exist_ok=True)
    (WORKSPACE / "data").mkdir(exist_ok=True)
