"""`lifecontext service` and `maintain`: unit rendering, dry runs and a fake service manager."""

from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import cli
from lifecontext.api.security import local_token
from lifecontext.paths import LifeContextPaths
from lifecontext.service import (
    CAPTURE_SERVICE,
    CAPTURE_TIMER,
    LAUNCHD_CAPTURE_LABEL,
    LAUNCHD_LABEL,
    LAUNCHD_MAINTAIN_LABEL,
    MAINTAIN_SERVICE,
    MAINTAIN_TIMER,
    SERVICE_UNIT,
    ServiceError,
    active_units,
    install,
    launchd_agents,
    render,
    restart_if_running,
    start_again,
    status,
    stop_units,
    systemd_units,
    uninstall,
)

EXECUTABLE = "/home/u/.local/share/life-context/venv/bin/lifecontext"
DEV_UNITS = ("life-context-dev-api.service", "lifeweave-dev-livesync.service")


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-service-test.", dir=scratch_root()) as temporary:
        yield Path(temporary).resolve(strict=True)


class _Manager:
    """Records commands instead of running systemctl or launchctl."""

    def __init__(self, outputs=None):
        self.commands: list[list[str]] = []
        self.outputs = outputs or {}

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        stdout, code = self.outputs.get(tuple(command), ("", 0))
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


class ServiceTests(unittest.TestCase):
    def test_systemd_units(self) -> None:
        units = systemd_units(EXECUTABLE, 8765, True)
        self.assertEqual(set(units), {SERVICE_UNIT, MAINTAIN_SERVICE, MAINTAIN_TIMER})
        self.assertIn(f"ExecStart={EXECUTABLE} serve --port 8765\n", units[SERVICE_UNIT])
        self.assertIn("Restart=on-failure\n", units[SERVICE_UNIT])
        self.assertIn(f"ExecStart={EXECUTABLE} maintain --json --port 8765\n", units[MAINTAIN_SERVICE])
        for limit in ("MemoryMax=512M", "CPUQuota=50%", "Nice=10", "Type=oneshot"):
            self.assertIn(limit + "\n", units[MAINTAIN_SERVICE])
        self.assertIn("OnCalendar=*-*-* 07:00:00\n", units[MAINTAIN_TIMER])
        self.assertIn("Persistent=true\n", units[MAINTAIN_TIMER])
        self.assertEqual(set(systemd_units(EXECUTABLE, 8765, False)), {SERVICE_UNIT})
        spaced = systemd_units("/home/u/My Apps/50%/lifecontext", 9000, False)[SERVICE_UNIT]
        self.assertIn('ExecStart="/home/u/My Apps/50%%/lifecontext" serve --port 9000\n', spaced)
        self.assertIn('Environment="PATH=/home/u/My Apps/50%%:/usr/local/bin:/usr/bin:/bin"\n', spaced)

    def test_serve_unit_limits_and_literal_dollar_signs(self) -> None:
        unit = systemd_units("/home/u/a$b/lifecontext", 8765, False)[SERVICE_UNIT]
        self.assertIn("ExecStart=/home/u/a$$b/lifecontext serve --port 8765\n", unit)
        # Environment= expands no variables, so the $ stays single there.
        self.assertIn("Environment=PATH=/home/u/a$b:/usr/local/bin:/usr/bin:/bin\n", unit)
        self.assertIn("MemoryHigh=4G\nNice=5\n", unit)
        self.assertNotIn("Environment=HOME=", unit)
        moved = systemd_units(EXECUTABLE, 8765, True, data_home=Path("/srv/life context"))
        for name in (SERVICE_UNIT, MAINTAIN_SERVICE):
            self.assertIn('Environment="HOME=/srv/life context"\n', moved[name])
        with _home() as home:
            agent = plistlib.loads(launchd_agents(home, EXECUTABLE, 8765, False, data_home=Path("/srv/data"))[
                f"{LAUNCHD_LABEL}.plist"])
            self.assertEqual(agent["EnvironmentVariables"]["HOME"], "/srv/data")

    def test_launchd_agents(self) -> None:
        with _home() as home:
            agents = launchd_agents(home, EXECUTABLE, 8765, True)
            serve = plistlib.loads(agents[f"{LAUNCHD_LABEL}.plist"])
            self.assertEqual(serve["ProgramArguments"], [EXECUTABLE, "serve", "--port", "8765"])
            self.assertEqual(serve["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(serve["Nice"], 5)
            self.assertTrue(serve["RunAtLoad"])
            maintain = plistlib.loads(agents[f"{LAUNCHD_MAINTAIN_LABEL}.plist"])
            self.assertEqual(maintain["StartCalendarInterval"], {"Hour": 7, "Minute": 0})
            self.assertEqual((maintain["Nice"], maintain["ProcessType"]), (10, "Background"))
            self.assertIn("/opt/homebrew/bin", maintain["EnvironmentVariables"]["PATH"])

    def test_systemd_install_dry_run_install_rerun_and_uninstall(self) -> None:
        with _home() as home:
            environ = {"XDG_CONFIG_HOME": str(home / "config")}
            units = home / "config" / "systemd" / "user"
            units.mkdir(parents=True)
            for name in DEV_UNITS:
                (units / name).write_text("[Service]\nExecStart=/bin/true\n")
            manager = _Manager()
            planned = install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=True,
                              runner=manager, environ=environ)
            self.assertEqual(manager.commands, [])
            self.assertEqual(sorted(path.name for path in units.iterdir()), sorted(DEV_UNITS))
            self.assertIn("Would write", render(planned))
            self.assertIn("ExecStart=", render(planned))

            install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                    runner=manager, environ=environ)
            self.assertEqual(manager.commands, [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "try-restart", SERVICE_UNIT],
                ["systemctl", "--user", "enable", "--now", SERVICE_UNIT],
                ["systemctl", "--user", "enable", "--now", MAINTAIN_TIMER],
            ])
            self.assertEqual((units / SERVICE_UNIT).read_text(), systemd_units(EXECUTABLE, 8765, True)[SERVICE_UNIT])
            manager.commands.clear()
            again = install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                            runner=manager, environ=environ)
            self.assertEqual({item["changed"] for item in again["files"]}, {False})
            self.assertNotIn(["systemctl", "--user", "try-restart", SERVICE_UNIT], manager.commands)

            manager.commands.clear()
            uninstall("Linux", home, maintenance_only=True, dry_run=False, runner=manager, environ=environ)
            self.assertEqual(manager.commands, [["systemctl", "--user", "disable", "--now", MAINTAIN_TIMER],
                                                ["systemctl", "--user", "daemon-reload"]])
            self.assertTrue((units / SERVICE_UNIT).exists())
            uninstall("Linux", home, maintenance_only=False, dry_run=False, runner=manager, environ=environ)
            self.assertEqual(sorted(path.name for path in units.iterdir()), sorted(DEV_UNITS))
            self.assertFalse(any(name in " ".join(command) for command in manager.commands for name in DEV_UNITS))
            nothing = uninstall("Linux", home, maintenance_only=False, dry_run=False, runner=manager, environ=environ)
            self.assertEqual((nothing["files"], nothing["commands"]), ([], []))

    def test_failed_manager_command_and_unsupported_platform(self) -> None:
        with _home() as home:
            failing = _Manager({("systemctl", "--user", "daemon-reload"): ("", 1)})
            with self.assertRaises(ServiceError) as caught:
                install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=False,
                        runner=failing, environ={"XDG_CONFIG_HOME": str(home)})
            self.assertEqual(caught.exception.code, "systemctl_failed")
            with self.assertRaises(ServiceError) as caught:
                install("Windows", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=True)
            self.assertEqual(caught.exception.code, "unsupported_platform")

    def test_launchd_install_and_status(self) -> None:
        with _home() as home:
            manager = _Manager({("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"): ("\tstate = running\n", 0),
                                ("launchctl", "print", f"gui/501/{LAUNCHD_MAINTAIN_LABEL}"): ("", 113)})
            install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                    runner=manager, uid=501)
            plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist)], manager.commands)
            report = status("Darwin", home, runner=manager, uid=501)
            self.assertEqual(report["units"][f"{LAUNCHD_LABEL}.plist"]["active"], "running")
            self.assertEqual(report["units"][f"{LAUNCHD_MAINTAIN_LABEL}.plist"]["active"], "not loaded")

    def test_launchd_reinstall_loads_an_unchanged_agent_that_is_not_loaded(self) -> None:
        with _home() as home:
            plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            failing = _Manager({("launchctl", "bootstrap", "gui/501", str(plist)): ("", 5)})
            with self.assertRaises(ServiceError) as caught:
                install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=False,
                        runner=failing, uid=501)
            self.assertEqual(caught.exception.code, "launchctl_failed")
            self.assertTrue(plist.is_file())
            unloaded = _Manager({("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"): ("", 113)})
            again = install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=False,
                            runner=unloaded, uid=501)
            self.assertEqual([item["changed"] for item in again["files"]], [False])
            self.assertEqual(again["commands"], [["launchctl", "bootstrap", "gui/501", str(plist)]])
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist)], unloaded.commands)
            loaded = _Manager()
            settled = install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=False,
                              runner=loaded, uid=501)
            self.assertEqual(settled["commands"], [])
            self.assertEqual(loaded.commands, [["launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"]])

    def test_no_start_writes_and_enables_but_starts_nothing(self) -> None:
        with _home() as home:
            environ = {"XDG_CONFIG_HOME": str(home / "config")}
            manager = _Manager()
            report = install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                             start=False, runner=manager, environ=environ)
            self.assertEqual(manager.commands, [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", SERVICE_UNIT],
                ["systemctl", "--user", "enable", MAINTAIN_TIMER],
            ])
            self.assertTrue((home / "config" / "systemd" / "user" / SERVICE_UNIT).is_file())
            self.assertIn("Not started", render(report))
            mac = _Manager()
            install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                    start=False, runner=mac, uid=501)
            self.assertEqual(mac.commands, [])
            self.assertTrue((home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").is_file())

    def test_uninstall_without_a_user_bus_still_removes_the_files(self) -> None:
        with _home() as home:
            environ = {"XDG_CONFIG_HOME": str(home / "config")}
            units = home / "config" / "systemd" / "user"
            install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, dry_run=False,
                    runner=_Manager(), environ=environ)
            no_bus = _Manager({("systemctl", "--user", "disable", "--now", MAINTAIN_TIMER): ("", 1),
                               ("systemctl", "--user", "disable", "--now", SERVICE_UNIT): ("", 1),
                               ("systemctl", "--user", "daemon-reload"): ("", 1)})
            report = uninstall("Linux", home, maintenance_only=False, dry_run=False, runner=no_bus, environ=environ)
            self.assertEqual(list(units.iterdir()), [])
            self.assertEqual(report["warnings"], [
                {"code": "systemctl_failed", "command": f"systemctl --user disable --now {MAINTAIN_TIMER}"},
                {"code": "systemctl_failed", "command": f"systemctl --user disable --now {SERVICE_UNIT}"},
                {"code": "systemctl_failed", "command": "systemctl --user daemon-reload"},
            ])
            self.assertIn("the unit files were removed anyway", render(report))

            install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=False, dry_run=False,
                    runner=_Manager(), environ=environ)

            def missing(command, **kwargs):
                raise FileNotFoundError(command[0])

            report = uninstall("Linux", home, maintenance_only=False, dry_run=False, runner=missing, environ=environ)
            self.assertEqual(list(units.iterdir()), [])
            self.assertEqual({warning["code"] for warning in report["warnings"]}, {"systemctl_unavailable"})

    def test_active_units_stop_and_start_again(self) -> None:
        with _home() as home:
            active = _Manager({("systemctl", "--user", "is-active", SERVICE_UNIT): ("active\n", 0)})
            self.assertEqual(active_units("Linux", runner=active), [SERVICE_UNIT])
            self.assertEqual(active.commands, [["systemctl", "--user", "is-active", SERVICE_UNIT]])
            stop_units("Linux", [SERVICE_UNIT], runner=active)
            self.assertEqual(active.commands[-1], ["systemctl", "--user", "stop", SERVICE_UNIT])
            start_again("Linux", home, [SERVICE_UNIT], runner=active, environ={})
            self.assertEqual(active.commands[-1], ["systemctl", "--user", "start", SERVICE_UNIT])
            failing = _Manager({("systemctl", "--user", "stop", SERVICE_UNIT): ("", 1)})
            with self.assertRaises(ServiceError):
                stop_units("Linux", [SERVICE_UNIT], runner=failing)
            inactive = _Manager({("systemctl", "--user", "is-active", SERVICE_UNIT): ("inactive\n", 3)})
            self.assertEqual(active_units("Linux", runner=inactive), [])

            def missing(command, **kwargs):
                raise FileNotFoundError(command[0])

            self.assertEqual(active_units("Linux", runner=missing), [])
            mac = _Manager()
            self.assertEqual(active_units("Darwin", runner=mac, uid=501), [f"{LAUNCHD_LABEL}.plist"])
            stop_units("Darwin", [f"{LAUNCHD_LABEL}.plist"], runner=mac, uid=501)
            self.assertEqual(mac.commands[-1], ["launchctl", "bootout", f"gui/501/{LAUNCHD_LABEL}"])
            start_again("Darwin", home, [f"{LAUNCHD_LABEL}.plist"], runner=mac, uid=501)
            self.assertEqual(mac.commands[-1], ["launchctl", "bootstrap", "gui/501",
                                                str(home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist")])
            self.assertEqual(active_units("Darwin", uid=501, runner=_Manager(
                {("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"): ("", 113)})), [])

    def test_status_reports_units_and_the_last_maintenance_run(self) -> None:
        with _home() as home:
            manager = _Manager({("systemctl", "--user", "is-active", SERVICE_UNIT): ("active\n", 0),
                                ("systemctl", "--user", "is-enabled", SERVICE_UNIT): ("enabled\n", 0)})
            paths = LifeContextPaths(home)
            paths.lifeweave_home.mkdir(mode=0o700)
            paths.maintenance_record_path.write_text(json.dumps({"finished_at": "2026-10-17T05:00:03Z", "ok": True}))
            report = status("Linux", home, runner=manager, environ={})
            self.assertEqual(report["units"][SERVICE_UNIT], {"installed": False, "active": "active", "enabled": "enabled"})
            self.assertTrue(report["last_maintenance"]["ok"])
            self.assertIn("Last maintenance: 2026-10-17T05:00:03Z (ok)", render(report))


    def test_capture_units_run_every_thirty_seconds_without_merging_runs(self) -> None:
        units = systemd_units(EXECUTABLE, 8765, False, capture=True)
        self.assertEqual(set(units), {SERVICE_UNIT, CAPTURE_SERVICE, CAPTURE_TIMER})
        self.assertIn(f"ExecStart={EXECUTABLE} capture --once --json --port 8765\n", units[CAPTURE_SERVICE])
        for line in ("Type=oneshot", f"After={SERVICE_UNIT}", "MemoryMax=512M", "Nice=10"):
            self.assertIn(line + "\n", units[CAPTURE_SERVICE])
        # The default accuracy of a minute would run 30-second timers only once a minute.
        for line in ("OnActiveSec=30s", "OnUnitActiveSec=30s", "AccuracySec=1s", f"Unit={CAPTURE_SERVICE}",
                     "WantedBy=timers.target"):
            self.assertIn(line + "\n", units[CAPTURE_TIMER])
        self.assertNotIn(CAPTURE_TIMER, systemd_units(EXECUTABLE, 8765, True))
        moved = systemd_units(EXECUTABLE, 8765, False, data_home=Path("/srv/life context"), capture=True)
        self.assertIn('Environment="HOME=/srv/life context"\n', moved[CAPTURE_SERVICE])
        with _home() as home:
            agents = launchd_agents(home, EXECUTABLE, 8765, False, capture=True)
            capture = plistlib.loads(agents[f"{LAUNCHD_CAPTURE_LABEL}.plist"])
            self.assertEqual(capture["ProgramArguments"], [EXECUTABLE, "capture", "--once", "--json", "--port", "8765"])
            self.assertEqual((capture["StartInterval"], capture["Nice"], capture["ProcessType"]),
                             (30, 10, "Background"))
            self.assertNotIn(f"{LAUNCHD_CAPTURE_LABEL}.plist", launchd_agents(home, EXECUTABLE, 8765, True))

    def test_install_status_and_uninstall_include_the_capture_timer(self) -> None:
        with _home() as home:
            environ = {"XDG_CONFIG_HOME": str(home / "config")}
            units = home / "config" / "systemd" / "user"
            manager = _Manager()
            planned = install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=False, capture=True,
                              dry_run=True, runner=manager, environ=environ)
            self.assertTrue(planned["capture"])
            self.assertEqual(manager.commands, [])
            self.assertFalse(units.exists())
            install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=True, capture=True, dry_run=False,
                    runner=manager, environ=environ)
            self.assertEqual(manager.commands[-2:], [["systemctl", "--user", "enable", "--now", MAINTAIN_TIMER],
                                                      ["systemctl", "--user", "enable", "--now", CAPTURE_TIMER]])
            self.assertEqual(sorted(path.name for path in units.iterdir()),
                             sorted([SERVICE_UNIT, MAINTAIN_SERVICE, MAINTAIN_TIMER, CAPTURE_SERVICE, CAPTURE_TIMER]))
            report = status("Linux", home, runner=manager, environ=environ)
            self.assertTrue(report["units"][CAPTURE_TIMER]["installed"])
            self.assertIn(f"{CAPTURE_TIMER}: installed", render(report))

            manager.commands.clear()
            only_capture = uninstall("Linux", home, maintenance_only=False, capture_only=True, dry_run=False,
                                     runner=manager, environ=environ)
            self.assertEqual([item["path"] for item in only_capture["files"]],
                             [str(units / CAPTURE_SERVICE), str(units / CAPTURE_TIMER)])
            self.assertEqual(manager.commands, [["systemctl", "--user", "disable", "--now", CAPTURE_TIMER],
                                                ["systemctl", "--user", "daemon-reload"]])
            self.assertEqual(sorted(path.name for path in units.iterdir()),
                             sorted([SERVICE_UNIT, MAINTAIN_SERVICE, MAINTAIN_TIMER]))
            install("Linux", home, executable=EXECUTABLE, port=8765, maintenance=False, capture=True, dry_run=False,
                    runner=manager, environ=environ)
            uninstall("Linux", home, maintenance_only=False, dry_run=False, runner=manager, environ=environ)
            self.assertEqual(list(units.iterdir()), [])

    def test_launchd_capture_agent_is_installed_and_removed(self) -> None:
        with _home() as home:
            manager = _Manager({("launchctl", "print", f"gui/501/{LAUNCHD_CAPTURE_LABEL}"): ("", 113)})
            install("Darwin", home, executable=EXECUTABLE, port=8765, maintenance=False, capture=True,
                    dry_run=False, runner=manager, uid=501)
            plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_CAPTURE_LABEL}.plist"
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist)], manager.commands)
            removed = uninstall("Darwin", home, maintenance_only=False, capture_only=True, dry_run=False,
                                runner=manager, uid=501)
            self.assertEqual([item["path"] for item in removed["files"]], [str(plist)])
            self.assertFalse(plist.exists())
            self.assertTrue((home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").exists())

    def test_restart_if_running_restarts_only_a_running_service(self) -> None:
        restart = ["systemctl", "--user", "restart", SERVICE_UNIT]
        probe = ("systemctl", "--user", "is-active", SERVICE_UNIT)
        manager = _Manager({probe: ("active\n", 0)})
        self.assertEqual(restart_if_running("Linux", runner=manager), {"state": "restarted", "command": restart})
        self.assertEqual(manager.commands, [list(probe), restart])
        manager = _Manager({probe: ("inactive\n", 3)})
        self.assertEqual(restart_if_running("Linux", runner=manager), {"state": "not_running", "command": restart})
        self.assertEqual(manager.commands, [list(probe)])
        # No user session bus: systemctl prints nothing on stdout.
        self.assertEqual(restart_if_running("Linux", runner=_Manager({probe: ("", 1)}))["state"], "no_manager")
        manager = _Manager({probe: ("active\n", 0), tuple(restart): ("", 1)})
        self.assertEqual(restart_if_running("Linux", runner=manager), {"state": "restart_failed", "command": restart})

        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])

        self.assertEqual(restart_if_running("Linux", runner=missing), {"state": "no_manager", "command": restart})
        self.assertEqual(restart_if_running("Windows"), {"state": "no_manager", "command": None})
        kick = ["launchctl", "kickstart", "-k", f"gui/501/{LAUNCHD_LABEL}"]
        loaded = _Manager({("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"): ("", 0)})
        self.assertEqual(restart_if_running("Darwin", runner=loaded, uid=501), {"state": "restarted", "command": kick})
        unloaded = _Manager({("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"): ("", 113)})
        self.assertEqual(restart_if_running("Darwin", runner=unloaded, uid=501)["state"], "not_running")


class _Api(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    lint: ClassVar[dict] = {"findings": [
        {"code": "stage_failed", "severity": "error", "source_id": "doc-a", "page_id": None,
         "detail": "wiki: failed (provider_unavailable)", "hint": "x"},
        {"code": "stage_failed", "severity": "error", "source_id": "doc-b", "page_id": None,
         "detail": "indexing: failed", "hint": "x"},
        {"code": "page_stale", "severity": "warning", "source_id": "doc-c", "page_id": "source_doc-c",
         "detail": None, "hint": "x"},
    ], "counts": {"error": 2, "warning": 1, "info": 0}, "omitted": 0}

    def log_message(self, *args):
        pass

    def _reply(self, body, operation=False):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if operation:
            self.send_header("X-Lifeweave-Operation-ID", "op_" + "1" * 32)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        type(self).requests.append(("GET", self.path, None, self.headers.get("Authorization")))
        self._reply(self.lint)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).requests.append(("POST", self.path, body, self.headers.get("Idempotency-Key")))
        if self.path.startswith("/v1/projections/reconcile"):
            self._reply({"examined": 4, "repaired": 1, "failed": 0})
        elif self.path.startswith("/v1/brief/project"):
            self._reply({"date": self.path.rsplit("=", 1)[1], "projection": {"brief": "completed", "log": "completed"}})
        else:
            self._reply({"operation_id": "op_" + "1" * 32, "state": "completed"}, operation=True)


class MaintainTests(unittest.TestCase):
    def _maintain(self, home: Path, *arguments: str) -> tuple[int, dict]:
        _Api.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = io.StringIO()
            with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(output):
                code = cli.main(["maintain", "--json", "--port", str(server.server_address[1]), *arguments])
        finally:
            server.shutdown()
            server.server_close()
        return code, json.loads(output.getvalue())

    def test_default_run_is_model_free_and_records_the_run(self) -> None:
        with _home() as home:
            paths = LifeContextPaths(home)
            local_token(paths, create=True)
            code, report = self._maintain(home, "--date", "2026-10-17")
            self.assertEqual(code, 0, report)
            self.assertEqual([(method, path) for method, path, *_ in _Api.requests], [
                ("POST", "/v1/projections/reconcile?limit=4096"),
                ("GET", "/v1/wiki/lint"),
                ("POST", "/v1/brief/project?date=2026-10-16"),
                ("POST", "/v1/brief/project?date=2026-10-17"),
            ])
            self.assertTrue(all(request[3].startswith("Bearer ") for request in _Api.requests if request[0] == "GET"))
            self.assertEqual(report["steps"]["lint"], {"error": 2, "warning": 1, "info": 0, "omitted": 0})
            self.assertEqual(report["steps"]["briefs"]["2026-10-16"], {"brief": "completed", "log": "completed"})
            self.assertNotIn("doc-a", json.dumps(report))
            record = json.loads(paths.maintenance_record_path.read_text())
            self.assertTrue(record["ok"])
            self.assertEqual(record["date"], "2026-10-17")

    def test_opt_in_rebuilds_are_bounded_and_idempotent_per_day(self) -> None:
        with _home() as home:
            local_token(LifeContextPaths(home), create=True)
            code, report = self._maintain(home, "--date", "2026-10-17", "--retry-wiki", "5", "--rebuild-stale", "1")
            self.assertEqual(code, 0, report)
            rebuilds = [(json.loads(body), key) for method, path, body, key in _Api.requests
                        if path == "/v1/wiki/rebuild"]
            self.assertEqual(rebuilds, [
                ({"source_id": "doc-a", "version": None}, "maintain-2026-10-17-retry-wiki-doc-a"),
                ({"source_id": "doc-c", "version": None}, "maintain-2026-10-17-rebuild-stale-doc-c"),
            ])
            self.assertEqual(report["steps"]["retry_wiki"], {"attempted": 1, "completed": 1, "failed": 0})
            self.assertEqual(report["steps"]["rebuild_stale"], {"attempted": 1, "completed": 1, "failed": 0})

    def test_unavailable_service_fails_and_is_recorded(self) -> None:
        with _home() as home:
            paths = LifeContextPaths(home)
            local_token(paths, create=True)
            output = io.StringIO()
            with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(output):
                code = cli.main(["maintain", "--json", "--port", "1"])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(report["steps"]["reconcile"], {"error": "api_unavailable"})
            self.assertFalse(json.loads(paths.maintenance_record_path.read_text())["ok"])


if __name__ == "__main__":
    unittest.main()
