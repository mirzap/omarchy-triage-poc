"""Clean-wheel and installed-workflow contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

from triage.pipeline import FIXTURES_DIR, load_prs_from_json

ROOT = Path(__file__).resolve().parents[1]


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def test_packaged_fixture_hunks_are_complete_and_canonical() -> None:
    assert FIXTURES_DIR == ROOT / "triage" / "fixtures"
    assert not list((ROOT / "fixtures").glob("*.json"))
    for name in ("prs.json", "newcomers.json"):
        prs = load_prs_from_json(FIXTURES_DIR / name)
        assert prs
        for pr in prs:
            pr.evidence_source = "fixtures"
            assert pr.revision_evidence().evidence_complete, (name, pr.number)


def test_clean_wheel_demo_restore_and_http_assets_outside_checkout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source)
    shutil.copy2(ROOT / "README.md", source)
    shutil.copytree(
        ROOT / "triage",
        source / "triage",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    dist = tmp_path / "dist"
    built = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist),
            str(source),
        ],
        cwd=tmp_path,
    )
    assert "Package would be ignored" not in built.stdout + built.stderr
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    required = {
        "triage/fixtures/prs.json",
        "triage/fixtures/newcomers.json",
        "triage/web/index.html",
        "triage/web/app.js",
        "triage/web/styles.css",
        "triage/web/vendor/virtual.js",
    }
    assert required <= names

    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = _venv_python(environment)
    clean_env = dict(os.environ)
    clean_env.pop("PYTHONPATH", None)
    clean_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    clean_env["PIP_NO_INDEX"] = "1"
    for key in ("TOGETHER_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
        clean_env.pop(key, None)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            str(wheel),
        ],
        cwd=tmp_path,
        env=clean_env,
    )

    workspace = tmp_path / "outside-checkout"
    workspace.mkdir()
    store = workspace / "workspace" / "store.json"
    first = _run(
        [str(python), "-I", "-m", "triage", "demo", "--store", str(store)],
        cwd=workspace,
        env=clean_env,
    )
    assert "NEEDS-HUMAN" in first.stdout
    assert store.is_file()
    assert not (workspace / ".triage" / "store.json").exists()

    backup = workspace / "validated-backup.json"
    _run(
        [
            str(python),
            "-I",
            "-m",
            "triage",
            "backup",
            "--store",
            str(store),
            "--output",
            str(backup),
        ],
        cwd=workspace,
        env=clean_env,
    )
    _run(
        [
            str(python),
            "-I",
            "-m",
            "triage",
            "demo",
            "--store",
            str(store),
            "--reset",
        ],
        cwd=workspace,
        env=clean_env,
    )
    _run(
        [
            str(python),
            "-I",
            "-m",
            "triage",
            "restore",
            str(backup),
            "--store",
            str(store),
            "--confirm-restore",
            str(store),
        ],
        cwd=workspace,
        env=clean_env,
    )
    assert store.with_name("store.json.pre-restore").is_file()
    restored = json.loads(store.read_text(encoding="utf-8"))
    assert restored["schema_version"] == 2

    asset_probe = """
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from triage.server import make_handler

server = ThreadingHTTPServer(
    ("127.0.0.1", 0),
    make_handler(Path(__import__("sys").argv[1]), allowed_hostnames=("127.0.0.1",)),
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    port = server.server_address[1]
    for path, marker in (("/app.js", b"Omarchy"), ("/vendor/virtual.js", b"Virtualizer")):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            payload = response.read()
            assert response.status == 200 and marker in payload
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
"""
    _run(
        [str(python), "-I", "-c", asset_probe, str(store)],
        cwd=workspace,
        env=clean_env,
    )

    invalid_provider = """
from triage.rank import RankError, provider
try:
    provider()
except RankError as exc:
    assert str(exc) == "embedding provider is not supported"
else:
    raise AssertionError("unsupported provider did not fail")
"""
    provider_env = dict(clean_env, EMBED_PROVIDER="unsupported")
    _run(
        [str(python), "-I", "-c", invalid_provider],
        cwd=workspace,
        env=provider_env,
    )
