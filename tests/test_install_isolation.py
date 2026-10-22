"""Prove the V1 package builds and installs without importing Hermes."""

from __future__ import annotations

import email.parser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from local_paths import scratch_root


V1_ROOT = Path(__file__).resolve().parents[1]
PYPI_INDEX = "https://pypi.org/simple"
PROJECT_VERSION = tomllib.loads((V1_ROOT / "pyproject.toml").read_text())["project"]["version"]


def _child_env(home: Path, tmpdir: Path) -> dict[str, str]:
    home.mkdir(mode=0o700)
    env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": PYPI_INDEX,
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "HERMES_HOME": str(home / "poisoned-hermes-home"),
    }
    return env


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class InstallIsolationTests(unittest.TestCase):
    def test_wheel_and_installed_command_are_independent_of_hermes(self) -> None:
        tmp_root = scratch_root()
        with tempfile.TemporaryDirectory(prefix="lifecontext-package-test.", dir=tmp_root) as tmp:
            work = Path(tmp).resolve(strict=True)
            home = work / "home"
            tmpdir = work / "tmp"
            tmpdir.mkdir(mode=0o700)
            env = _child_env(home, tmpdir)

            # Build an exact copy of the v1 project in disposable space. This
            # keeps setuptools scratch, metadata, and wheel output out of the
            # source checkout while exercising the package configuration.
            project = work / "project"
            (project / "src").mkdir(parents=True)
            # pyproject.toml names LICENSE and NOTICE as license files.
            for name in ("pyproject.toml", "LICENSE", "NOTICE"):
                shutil.copy2(V1_ROOT / name, project / name)
            shutil.copytree(V1_ROOT / "src", project / "src", dirs_exist_ok=True)
            wheel_dir = work / "wheelhouse"
            wheel_dir.mkdir()

            built = _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--isolated",
                    "--no-deps",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--index-url",
                    PYPI_INDEX,
                    "--wheel-dir",
                    str(wheel_dir),
                    ".",
                ],
                cwd=project,
                env=env,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            wheels = list(wheel_dir.glob("lifecontext-*.whl"))
            self.assertEqual(len(wheels), 1, "expected one LifeContext wheel")

            with zipfile.ZipFile(wheels[0]) as wheel:
                members = set(wheel.namelist())
                self.assertIn("lifecontext/__init__.py", members)
                self.assertIn("lifecontext/cli.py", members)
                for module in (
                    "application",
                    "catalog",
                    "custody",
                    "hindsight",
                    "hindsight_adapter",
                    "vault",
                ):
                    self.assertIn(f"lifecontext/{module}.py", members)
                self.assertTrue(
                    all(
                        name.startswith(("lifecontext/", f"lifecontext-{PROJECT_VERSION}.dist-info/"))
                        for name in members
                    ),
                    f"wheel contains files outside the LifeContext distribution: {sorted(members)}",
                )
                metadata_name = next(
                    name for name in members if name.endswith(".dist-info/METADATA")
                )
                metadata = email.parser.Parser().parsestr(
                    wheel.read(metadata_name).decode("utf-8")
                )
            self.assertEqual(metadata.get("Name"), "lifecontext")
            self.assertEqual(metadata.get("Version"), PROJECT_VERSION)
            self.assertEqual(
                set(metadata.get_all("Requires-Dist", [])),
                {
                    "fastapi==0.141.1",
                    "hindsight-all==0.9.2",
                    "hindsight-client==0.9.2",
                    "pydantic==2.13.5",
                    "PyYAML==6.0.3",
                    "uvicorn==0.53.0",
                    "pypdf==6.19.0",
                    "pypdfium2==5.14.0",
                    "python-docx==1.2.0",
                    "openpyxl==3.1.5",
                    "python-pptx==1.0.2",
                    "Pillow==13.0.0",
                    "defusedxml==0.7.1",
                    "anthropic==1.4.0",
                    "openai==2.54.0",
                    "google-genai==2.22.0",
                    "mcp==2.2.0",
                },
            )

            venv = work / "venv"
            created = _run([sys.executable, "-m", "venv", str(venv)], cwd=work, env=env)
            self.assertEqual(created.returncode, 0, created.stderr)
            python = venv / "bin" / "python"
            uv = shutil.which("uv")
            install_env = dict(env)
            if uv:
                install_env["UV_CACHE_DIR"] = str(work / "uv-cache")
                install_args = [
                    uv,
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    str(python),
                    "--index-url",
                    PYPI_INDEX,
                    str(wheels[0]),
                ]
            else:
                install_args = [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--isolated",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--index-url",
                    PYPI_INDEX,
                    str(wheels[0]),
                ]
            installed = _run(
                install_args,
                cwd=work,
                env=install_env,
                timeout=600,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            run_dir = work / "run"
            run_dir.mkdir()
            command = venv / "bin" / "lifecontext"
            help_result = _run([str(command), "--help"], cwd=run_dir, env=env)
            version_result = _run([str(command), "--version"], cwd=run_dir, env=env)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: lifecontext", help_result.stdout.lower())
            self.assertEqual(version_result.returncode, 0, version_result.stderr)
            self.assertEqual(version_result.stdout.strip(), f"lifecontext {PROJECT_VERSION}")
            alias_result = _run([str(venv / "bin" / "life-context"), "--version"], cwd=run_dir, env=env)
            self.assertEqual(alias_result.stdout.strip(), f"lifecontext {PROJECT_VERSION}")

            probe = _run(
                [
                    str(python),
                    "-c",
                    """import importlib, importlib.metadata as md, importlib.util, json, lifecontext
from pathlib import Path
modules = ("application", "catalog", "custody", "hindsight", "hindsight_adapter", "vault")
module_origins = {
    name: str(Path(importlib.import_module(f"lifecontext.{name}").__file__).resolve())
    for name in modules
}
print(json.dumps({
    "origin": str(Path(lifecontext.__file__).resolve()),
    "module_origins": module_origins,
    "hermes_cli": importlib.util.find_spec("hermes_cli"),
    "run_agent": importlib.util.find_spec("run_agent"),
    "dists": sorted(d.metadata["Name"].lower() for d in md.distributions() if d.metadata.get("Name")),
}))""",
                ],
                cwd=run_dir,
                env=env,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            result = json.loads(probe.stdout)
            self.assertTrue(Path(result["origin"]).is_relative_to(venv.resolve()))
            self.assertEqual(
                set(result["module_origins"]),
                {"application", "catalog", "custody", "hindsight", "hindsight_adapter", "vault"},
            )
            for origin in result["module_origins"].values():
                self.assertTrue(Path(origin).is_relative_to(venv.resolve()), origin)
            self.assertIsNone(result["hermes_cli"])
            self.assertIsNone(result["run_agent"])
            self.assertNotIn("hermes-agent", result["dists"])


if __name__ == "__main__":
    unittest.main()
