"""Fail if a tracked file looks like it contains a credential. Offline, with no dependencies."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "private_key_hex": re.compile(
        r"(?i)(private[_-]?key|secret|passphrase)\s*[:=]\s*['\"]?(0x)?[0-9a-f]{32,}"
    ),
    "pem_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assigned_api_secret": re.compile(r"(?i)api[_-]?secret\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"),
    "mnemonic_assignment": re.compile(
        r"(?i)(mnemonic|seed[_ ]phrase)\s*[:=]\s*['\"]?([a-z]{3,8} ){11,}"
    ),
}
SKIP_SUFFIXES = {".parquet", ".lock", ".png", ".svg"}
SKIP_FILES = {"scripts/check_secrets.py", "tests/unit/test_ci_hygiene.py"}


def tracked_files() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in done.stdout.splitlines() if line]


def scan(paths: list[str]) -> list[str]:
    findings = []
    for name in paths:
        path = Path(name)
        if name in SKIP_FILES or path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {label}")
    return findings


def main() -> int:
    findings = scan(tracked_files())
    for finding in findings:
        print(f"POSSIBLE SECRET: {finding}")
    print(f"secret scan: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
