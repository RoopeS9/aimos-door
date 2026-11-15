"""UI-owned acceptance cases K10, K11, L09 and M05, executed in headless Chromium."""
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


def test_K10():
    """An approved hold shows as committed, without "Why this needs you" or "not committed"."""
    rendered("K10")


def test_K11():
    """The Record view reveals the model's probabilities from options_considered lines."""
    rendered("K11")


def test_L09():
    """The Chat view is a conversation: transcript, history, products, draft card."""
    rendered("L09")


def test_M05():
    """The Limits view changes a signed ceiling in three steps with a simulated identification."""
    rendered("M05")


def test_M06():
    """Approvals, proposals and limit changes identify through a mock bank window."""
    rendered("M06")


def test_N04():
    """A refused sign-off says why in place; an undeliverable hold can only be rejected or changed."""
    rendered("N04")
