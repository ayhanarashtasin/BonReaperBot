"""BR-008: import sanity, secret scanning and CI safety properties."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

import bone_reaper

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_secrets  # noqa: E402


def test_every_module_imports_without_credentials_or_network():
    names = [
        m.name
        for m in pkgutil.walk_packages(bone_reaper.__path__, "bone_reaper.")
        if not m.name.endswith("__main__")
    ]
    assert len(names) > 40
    for name in names:
        importlib.import_module(name)


@pytest.mark.parametrize(
    "text",
    [
        "private_key = '" + "ab" * 32 + "'",
        "-----BEGIN RSA PRIVATE KEY-----",
        'api_secret = "abcdefghijklmnopqrstuvwxyz"',
        "mnemonic: " + "word " * 12,
    ],
)
def test_secret_scanner_catches_credentials(tmp_path, monkeypatch, text):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "leak.txt").write_text(text)
    assert check_secrets.scan(["leak.txt"])


def test_secret_scanner_passes_normal_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ok.py").write_text('hash = "' + "ab" * 32 + '"\nprice = "0.50"\n')
    assert check_secrets.scan(["ok.py"]) == []


def test_committed_tree_has_no_secrets(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert check_secrets.scan(check_secrets.tracked_files()) == []


def test_ci_workflow_has_no_secrets_and_runs_no_metered_tests():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "${{ secrets" not in workflow
    assert "metered" not in workflow and "--run-network" not in workflow


def test_metered_marker_is_never_collected(pytester=None):
    conftest = (ROOT / "tests/conftest.py").read_text()
    assert 'if "metered" in item.keywords' in conftest


def test_lockfile_pins_official_sdk_and_not_the_legacy_client():
    lock = (ROOT / "uv.lock").read_text()
    assert 'name = "polymarket-client"' in lock and 'version = "0.17.0"' in lock
    assert "py-clob-client" not in lock
