"""Scan history, client bundle and logs without ever displaying secret values."""
from __future__ import annotations

from pathlib import Path
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def scan() -> list[str]:
    private = dotenv_values(ROOT / ".env")
    values = [str(v).encode() for k, v in private.items() if v and ("KEY" in k or "SECRET" in k or "TOKEN" in k or "ACCESS_CODE" in k) and len(str(v)) >= 8]
    findings = []
    history = subprocess.run(["git", "rev-list", "--all", "--objects"], cwd=ROOT, check=True, capture_output=True).stdout.splitlines()
    for entry in history:
        oid = entry.split(b" ", 1)[0].decode()
        content = subprocess.run(["git", "cat-file", "-p", oid], cwd=ROOT, check=True, capture_output=True).stdout
        if any(value in content for value in values):
            findings.append("A configured secret occurs in a git object (value withheld).")
    paths = list((ROOT / "frontend" / "dist").rglob("*")) + list(ROOT.glob("*.log")) + list((ROOT / "data").rglob("*.log"))
    for path in paths:
        if path.is_file() and any(value in path.read_bytes() for value in values):
            findings.append(f"A configured secret occurs in {path.relative_to(ROOT)} (value withheld).")
    ignored = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True)
    if ignored.returncode != 0:
        findings.append(".env is not gitignored.")
    tracked = subprocess.run(["git", "ls-files", "--", ".env"], cwd=ROOT, capture_output=True, check=True)
    if tracked.stdout.strip():
        findings.append(".env is tracked.")
    return findings


if __name__ == "__main__":
    findings = scan()
    print("\n".join(findings) if findings else "PASS: no configured secret found; .env is ignored and untracked.")
    raise SystemExit(bool(findings))
