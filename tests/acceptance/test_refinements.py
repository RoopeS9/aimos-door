"""UI-owned acceptance cases J01–J04, executed in headless Chromium."""
import os
import subprocess

from conftest import ROOT


def rendered(case_id):
    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/aimos-playwright-browsers")
    result = subprocess.run(
        ["npm", "run", "test:e2e", "--", "--grep", f"@{case_id}\\b"],
        cwd=ROOT / "frontend", env=env, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, "The requested case must execute in Chromium, not merely collect."


def test_J01():
    rendered("J01")


def test_J02():
    rendered("J02")


def test_J03():
    rendered("J03")


def test_J04():
    rendered("J04")
