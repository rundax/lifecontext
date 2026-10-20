"""``lifecontext service``: run the API, and optionally daily maintenance, as a user service.

Linux uses ``systemd --user`` units, macOS launchd agents. Only the files named
here are ever written or removed; other units (for example a development
checkout's ``life-context-dev-*``) are never touched.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lifecontext.paths import LifeContextPaths, atomic_write

SERVICE_UNIT = "lifecontext.service"
MAINTAIN_SERVICE = "lifecontext-maintain.service"
MAINTAIN_TIMER = "lifecontext-maintain.timer"
LAUNCHD_LABEL = "io.github.hantok.lifecontext"
LAUNCHD_MAINTAIN_LABEL = "io.github.hantok.lifecontext.maintain"
MAINTENANCE_HOUR = 7
# The maintenance run is an HTTP client of the service; these bound the client process.
MAINTAIN_LIMITS = {"MemoryMax": "512M", "CPUQuota": "50%", "Nice": "10"}
# The API, its Hindsight daemon and Postgres share the service's control group: past
# MemoryHigh the kernel reclaims and throttles instead of starving the desktop, and a
# mild Nice keeps interactive work first. No hard cap: recall needs its models in memory.
SERVE_LIMITS = {"MemoryHigh": "4G", "Nice": "5"}
_DOCS = "https://github.com/Hantok/lifecontext/blob/main/docs/operations.md"

Runner = Callable[..., Any]


class ServiceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def manager_for(system: str) -> str:
    if system == "Linux":
        return "systemd"
    if system == "Darwin":
        return "launchd"
    raise ServiceError("unsupported_platform")


def lifecontext_executable() -> str:
    """The ``lifecontext`` next to this interpreter (the install's venv), else the one on PATH."""
    candidate = Path(sys.executable).parent / "lifecontext"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which("lifecontext")
    if found is None:
        raise ServiceError("executable_not_found")
    return os.path.abspath(found)


def _search_path(executable: str, manager: str) -> str:
    system_dirs = ["/opt/homebrew/bin"] if manager == "launchd" else []
    return ":".join([str(Path(executable).parent), *system_dirs, "/usr/local/bin", "/usr/bin", "/bin"])


def account_home() -> Path:
    """The account's home from the password database, where systemd and launchd look for user units.

    A HOME overridden for LifeContext data does not move the units; the units pass that HOME on instead.
    """
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return Path.home()


def _systemd_word(value: str, *, assignment: bool = False) -> str:
    """One ExecStart word or Environment= assignment, quoted when needed.

    systemd expands specifiers (%) in both but variables ($) only in ExecStart,
    so a literal $ is doubled only there.
    """
    escaped = value.replace("%", "%%") if assignment else value.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./$-]+", escaped):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _environment(executable: str, manager: str, data_home: Path | None) -> dict[str, str]:
    return {"PATH": _search_path(executable, manager), **({"HOME": str(data_home)} if data_home else {})}


def systemd_units(executable: str, port: int, maintenance: bool, *,
                  data_home: Path | None = None) -> dict[str, str]:
    """``data_home`` becomes ``Environment=HOME=`` when the data lives under a HOME other than the account's."""
    environment = [f"Environment={_systemd_word(f'{name}={value}', assignment=True)}"
                   for name, value in _environment(executable, "systemd", data_home).items()]
    command = " ".join(_systemd_word(word) for word in (executable, "serve", "--port", str(port)))
    units = {SERVICE_UNIT: "\n".join([
        "[Unit]",
        "Description=LifeContext API on loopback",
        f"Documentation={_DOCS}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={command}",
        *environment,
        "Environment=PYTHONUNBUFFERED=1",
        *(f"{name}={value}" for name, value in SERVE_LIMITS.items()),
        "Restart=on-failure",
        "RestartSec=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])}
    if maintenance:
        maintain = " ".join(_systemd_word(word) for word in (executable, "maintain", "--json", "--port", str(port)))
        units[MAINTAIN_SERVICE] = "\n".join([
            "[Unit]",
            "Description=LifeContext daily maintenance: reconcile, lint, briefs (no model calls)",
            f"Documentation={_DOCS}",
            f"After={SERVICE_UNIT}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={maintain}",
            *environment,
            *(f"{name}={value}" for name, value in MAINTAIN_LIMITS.items()),
            "",
        ])
        units[MAINTAIN_TIMER] = "\n".join([
            "[Unit]",
            f"Description=Run LifeContext maintenance daily at {MAINTENANCE_HOUR:02d}:00 local time",
            "",
            "[Timer]",
            f"OnCalendar=*-*-* {MAINTENANCE_HOUR:02d}:00:00",
            "Persistent=true",
            f"Unit={MAINTAIN_SERVICE}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])
    return units


def launchd_agents(home: Path, executable: str, port: int, maintenance: bool, *,
                   data_home: Path | None = None) -> dict[str, bytes]:
    logs = home / "Library" / "Logs" / "lifecontext"
    environment = _environment(executable, "launchd", data_home)
    agents = {f"{LAUNCHD_LABEL}.plist": plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [executable, "serve", "--port", str(port)],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "Nice": int(SERVE_LIMITS["Nice"]),
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "serve.log"),
        "StandardErrorPath": str(logs / "serve.log"),
    })}
    if maintenance:
        # launchd has no memory or CPU caps; a low priority stands in for them.
        agents[f"{LAUNCHD_MAINTAIN_LABEL}.plist"] = plistlib.dumps({
            "Label": LAUNCHD_MAINTAIN_LABEL,
            "ProgramArguments": [executable, "maintain", "--json", "--port", str(port)],
            "EnvironmentVariables": environment,
            "StartCalendarInterval": {"Hour": MAINTENANCE_HOUR, "Minute": 0},
            "Nice": int(MAINTAIN_LIMITS["Nice"]),
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "StandardOutPath": str(logs / "maintain.log"),
            "StandardErrorPath": str(logs / "maintain.log"),
        })
    return agents


def _unit_directory(home: Path, manager: str, environ: Mapping[str, str]) -> Path:
    if manager == "launchd":
        return home / "Library" / "LaunchAgents"
    config = Path(environ["XDG_CONFIG_HOME"]) if environ.get("XDG_CONFIG_HOME") else home / ".config"
    return config / "systemd" / "user"


def _our_files(manager: str) -> tuple[str, ...]:
    if manager == "launchd":
        return (f"{LAUNCHD_LABEL}.plist", f"{LAUNCHD_MAINTAIN_LABEL}.plist")
    return (SERVICE_UNIT, MAINTAIN_SERVICE, MAINTAIN_TIMER)


def _run(runner: Runner, command: list[str], *, allowed: bool = False,
         timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise ServiceError(f"{command[0]}_unavailable") from None
    except (OSError, subprocess.SubprocessError):
        raise ServiceError(f"{command[0]}_failed") from None
    if result.returncode != 0 and not allowed:
        raise ServiceError(f"{command[0]}_failed")
    return result


def _loaded(runner: Runner, domain: str, label: str) -> bool:
    try:
        result = runner(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True,
                        timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install(system: str, home: Path, *, executable: str, port: int, maintenance: bool, dry_run: bool,
            start: bool = True, data_home: Path | None = None,
            runner: Runner = subprocess.run, environ: Mapping[str, str] | None = None,
            uid: int | None = None) -> dict[str, Any]:
    """Write the units under ``home`` (the account's) and start them unless ``start`` is false.

    Without ``start`` systemd units are enabled for the next login and launchd agents
    load at the next login; nothing is started or restarted now.
    """
    environ = os.environ if environ is None else environ
    manager = manager_for(system)
    if not os.path.isabs(executable):
        raise ServiceError("executable_not_found")
    directory = _unit_directory(home, manager, environ)
    content = ({name: text.encode() for name, text in
                systemd_units(executable, port, maintenance, data_home=data_home).items()}
               if manager == "systemd" else launchd_agents(home, executable, port, maintenance, data_home=data_home))
    files = []
    for name, data in content.items():
        path = directory / name
        try:
            changed = path.read_bytes() != data
        except OSError:
            changed = True
        files.append({"path": str(path), "changed": changed, "content": data.decode()})
    commands: list[list[str]] = []
    if manager == "systemd":
        enable = ["systemctl", "--user", "enable", *(["--now"] if start else [])]
        commands.append(["systemctl", "--user", "daemon-reload"])
        if start and any(item["changed"] for item in files if item["path"].endswith(SERVICE_UNIT)):
            commands.append(["systemctl", "--user", "try-restart", SERVICE_UNIT])
        commands.append([*enable, SERVICE_UNIT])
        if maintenance:
            commands.append([*enable, MAINTAIN_TIMER])
    elif start:
        domain = f"gui/{os.getuid() if uid is None else uid}"
        for item in files:
            label = Path(item["path"]).stem
            if item["changed"]:
                commands.append(["launchctl", "bootout", f"{domain}/{label}"])
                commands.append(["launchctl", "bootstrap", domain, item["path"]])
            elif not _loaded(runner, domain, label):
                # Unchanged but not loaded, for example after a first bootstrap failed.
                commands.append(["launchctl", "bootstrap", domain, item["path"]])
    report = {"command": "install", "manager": manager, "dry_run": dry_run, "files": files,
              "commands": commands, "port": port, "maintenance": maintenance, "start": start}
    if dry_run:
        return report
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if manager == "launchd":
            (home / "Library" / "Logs" / "lifecontext").mkdir(mode=0o700, parents=True, exist_ok=True)
        for item in files:
            if item["changed"]:
                atomic_write(Path(item["path"]), item["content"].encode(), mode=0o644)
    except OSError:
        raise ServiceError("unit_write_failed") from None
    for command in commands:
        # bootout of an agent that is not loaded yet fails harmlessly.
        _run(runner, command, allowed=command[:2] == ["launchctl", "bootout"])
    return report


def uninstall(system: str, home: Path, *, maintenance_only: bool, dry_run: bool,
              runner: Runner = subprocess.run, environ: Mapping[str, str] | None = None,
              uid: int | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    manager = manager_for(system)
    directory = _unit_directory(home, manager, environ)
    names = _our_files(manager)
    if maintenance_only:
        names = names[1:]
    present = [directory / name for name in names if os.path.lexists(directory / name)]
    commands: list[list[str]] = []
    if manager == "systemd":
        for name in (MAINTAIN_TIMER, SERVICE_UNIT):
            if directory / name in present:
                commands.append(["systemctl", "--user", "disable", "--now", name])
        if present:
            commands.append(["systemctl", "--user", "daemon-reload"])
    else:
        domain = f"gui/{os.getuid() if uid is None else uid}"
        commands += [["launchctl", "bootout", f"{domain}/{path.stem}"] for path in present]
    warnings: list[dict[str, str]] = []
    report = {"command": "uninstall", "manager": manager, "dry_run": dry_run,
              "files": [{"path": str(path)} for path in present], "commands": commands, "warnings": warnings}
    if dry_run:
        return report

    def attempt(command: list[str]) -> None:
        # Without a user session bus (ssh, sudo, containers) systemctl fails; the files still go.
        try:
            result = _run(runner, command, allowed=True)
        except ServiceError as exc:
            warnings.append({"code": exc.code, "command": " ".join(command)})
            return
        # bootout of an agent that is not loaded fails harmlessly.
        if result.returncode != 0 and command[0] != "launchctl":
            warnings.append({"code": f"{command[0]}_failed", "command": " ".join(command)})

    reload = [command for command in commands if command[-1] == "daemon-reload"]
    for command in commands:
        if command not in reload:
            attempt(command)
    try:
        for path in present:
            path.unlink()
    except OSError:
        raise ServiceError("unit_remove_failed") from None
    for command in reload:
        attempt(command)
    return report


def active_units(system: str, *, runner: Runner = subprocess.run, uid: int | None = None) -> list[str]:
    """LifeContext's API unit when it runs (systemd) or is loaded (launchd); else nothing.

    Nothing is returned when the service manager is absent or unreachable.
    """
    if manager_for(system) == "systemd":
        try:
            result = runner(["systemctl", "--user", "is-active", SERVICE_UNIT], capture_output=True, text=True,
                            timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        return [SERVICE_UNIT] if (result.stdout or "").strip() in {"active", "activating", "reloading"} else []
    domain = f"gui/{os.getuid() if uid is None else uid}"
    return [f"{LAUNCHD_LABEL}.plist"] if _loaded(runner, domain, LAUNCHD_LABEL) else []


def stop_units(system: str, units: list[str], *, runner: Runner = subprocess.run, uid: int | None = None) -> None:
    """Stop what ``active_units`` returned; ``start_again`` starts it."""
    manager = manager_for(system)
    for name in units:
        if manager == "systemd":
            # systemctl stop waits for the unit, including the Hindsight daemon and Postgres in its group.
            _run(runner, ["systemctl", "--user", "stop", name], timeout=180)
        else:
            domain = f"gui/{os.getuid() if uid is None else uid}"
            _run(runner, ["launchctl", "bootout", f"{domain}/{name.removesuffix('.plist')}"], timeout=180)


def start_again(system: str, home: Path, units: list[str], *, runner: Runner = subprocess.run,
                environ: Mapping[str, str] | None = None, uid: int | None = None) -> None:
    """Start what ``stop_running`` stopped."""
    environ = os.environ if environ is None else environ
    manager = manager_for(system)
    for name in units:
        if manager == "systemd":
            _run(runner, ["systemctl", "--user", "start", name])
        else:
            domain = f"gui/{os.getuid() if uid is None else uid}"
            _run(runner, ["launchctl", "bootstrap", domain, str(_unit_directory(home, manager, environ) / name)])


def read_maintenance_record(paths: LifeContextPaths) -> dict[str, Any] | None:
    try:
        value = json.loads(paths.maintenance_record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def status(system: str, home: Path, *, runner: Runner = subprocess.run,
           environ: Mapping[str, str] | None = None, uid: int | None = None,
           data_home: Path | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    manager = manager_for(system)
    directory = _unit_directory(home, manager, environ)
    units: dict[str, dict[str, Any]] = {}
    for name in _our_files(manager):
        entry: dict[str, Any] = {"installed": (directory / name).is_file()}
        try:
            if manager == "systemd":
                for query in ("is-active", "is-enabled"):
                    result = runner(["systemctl", "--user", query, name], capture_output=True, text=True,
                                    timeout=30, check=False)
                    entry[query.removeprefix("is-")] = (result.stdout or "").strip() or "unknown"
            else:
                label = name.removesuffix(".plist")
                domain = f"gui/{os.getuid() if uid is None else uid}"
                result = runner(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True,
                                timeout=30, check=False)
                state = re.search(r"^\s*state = (\S+)", result.stdout or "", re.MULTILINE)
                entry["loaded"] = result.returncode == 0
                entry["active"] = state.group(1) if state else ("not loaded" if result.returncode else "unknown")
        except (OSError, subprocess.SubprocessError):
            entry["active"] = "unknown"
        units[name] = entry
    return {"command": "status", "manager": manager, "units": units,
            "last_maintenance": read_maintenance_record(LifeContextPaths(data_home or home))}


def render(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    prefix = "Would " if report.get("dry_run") else ""
    if report["command"] == "status":
        for name, entry in report["units"].items():
            installed = "installed" if entry["installed"] else "not installed"
            lines.append(f"{name}: {installed}, {entry.get('active', 'unknown')}"
                         + (f", {entry['enabled']}" if "enabled" in entry else ""))
        last = report["last_maintenance"]
        lines.append("Last maintenance: " + ("never" if last is None else
                                             f"{last.get('finished_at')} ({'ok' if last.get('ok') else 'failed'})"))
        return "\n".join(lines)
    verb = "write" if report["command"] == "install" else "remove"
    for item in report["files"]:
        if report["command"] == "install" and not item["changed"]:
            lines.append(f"Unchanged: {item['path']}")
        else:
            lines.append(f"{prefix or ''}{verb.capitalize() if not prefix else verb}: {item['path']}")
            if report.get("dry_run") and report["command"] == "install":
                lines += ["    " + line for line in item["content"].rstrip("\n").splitlines()]
    for command in report["commands"]:
        lines.append(f"{prefix}{'run' if prefix else 'Ran'}: {' '.join(command)}")
    if not report["files"]:
        lines.append("Nothing to remove." if report["command"] == "uninstall" else "Nothing to do.")
    for warning in report.get("warnings", []):
        lines.append(f"Warning: {warning['command']} failed ({warning['code']}); the unit files were removed anyway.")
    if report["command"] == "install" and not report.get("start", True):
        lines.append("Not started: it starts at your next login, or now with lifecontext service install.")
    if report["command"] == "install" and report["manager"] == "systemd" and not report.get("dry_run"):
        lines.append("To keep it running after you log out: loginctl enable-linger \"$USER\"")
    return "\n".join(lines)
