"""Weeek's browser channel: login via Playwright.

The browser runs only to (re)acquire the session cookies the internal KB API
needs — data itself is fetched with httpx, and bodies are written over the
collaborative channel (see ``collab.py``). Weeek's login is a two-step flow:
enter email -> Continue -> enter password -> submit. The saved storageState
(cookies) is then reused by the httpx client.
"""

from __future__ import annotations

import asyncio
import json
import time

from ..config import Config
from ..logging_util import make_logger

_LOGIN_TIMEOUT = 45.0  # hard ceiling so a stuck browser fails loudly instead of hanging

LOGIN_PATH = "/login"  # redirects to /welcome
EMAIL_INPUT = "input[type='email'], input[name='email']"
PASSWORD_INPUT = "input[type='password'], input[name='password']"
# Buttons carry localized text; we match several and also submit via Enter.
CONTINUE_LABELS = ["Continue", "Продолжить", "Далее", "Next"]
SUBMIT_LABELS = ["Continue", "Log in", "Sign in", "Войти", "Войти в аккаунт", "Продолжить"]
# URLs that mean we are NOT authenticated yet.
UNAUTH_MARKERS = ("/login", "/welcome", "/sign-up")


class KBAuthError(RuntimeError):
    """Not logged in and unable to log in (missing credentials, 2FA, captcha, or SSO)."""


def load_cookies_into(client, cfg: Config) -> int:
    """Load storageState cookies into an httpx client. Returns the count loaded."""
    path = cfg.storage_state_path
    if not path.exists():
        return 0
    state = json.loads(path.read_text())
    count = 0
    for c in state.get("cookies", []):
        if "weeek.net" not in c.get("domain", ""):
            continue
        client.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
        count += 1
    return count


async def automated_login(cfg: Config) -> None:
    """Log in headlessly with WEEEK_EMAIL/WEEEK_PASSWORD and save storageState.

    Raises KBAuthError if credentials are missing or login does not complete
    (typically 2FA, captcha, or Google/SSO) — the caller should fall back to the
    interactive ``weeek-mcp-login`` seeder. Bounded by ``_LOGIN_TIMEOUT`` so a stuck
    browser (e.g. launch hanging) fails with a clear error instead of hanging past
    the MCP client's own tool-call timeout with no trace of why.
    """
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(_automated_login(cfg), timeout=_LOGIN_TIMEOUT)
    except TimeoutError as exc:
        raise KBAuthError(
            f"Login timed out after {_LOGIN_TIMEOUT:.0f}s (stuck at {time.monotonic() - t0:.1f}s in)."
        ) from exc


async def _automated_login(cfg: Config) -> None:
    log = make_logger(cfg.log_path, "weeek-mcp/kb")
    if not cfg.has_kb_credentials:
        raise KBAuthError(
            "Not logged in and WEEEK_EMAIL/WEEEK_PASSWORD are not set. "
            "Run `weeek-mcp-login` once to sign in interactively and cache the session."
        )
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=cfg.headless)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            t0 = time.monotonic()
            await page.goto(cfg.app_base + LOGIN_PATH, wait_until="domcontentloaded", timeout=15000)
            log(f"login: reached {LOGIN_PATH} in {time.monotonic() - t0:.1f}s")
            await page.wait_for_timeout(2000)

            # Step 1: email -> Continue
            await page.fill(EMAIL_INPUT, cfg.email or "")
            if await page.query_selector(PASSWORD_INPUT) is None:
                for label in CONTINUE_LABELS:
                    btn = page.get_by_role("button", name=label)
                    if await btn.count():
                        await btn.first.click()
                        await page.wait_for_timeout(1500)
                        break

            # Step 2: password -> submit (Enter, plus a labelled-button fallback)
            await page.fill(PASSWORD_INPUT, cfg.password or "")
            await page.focus(PASSWORD_INPUT)
            await page.keyboard.press("Enter")
            for label in SUBMIT_LABELS:
                btn = page.get_by_role("button", name=label)
                if await btn.count():
                    try:
                        await btn.first.click(timeout=3000)
                    except Exception:
                        pass
                    break

            try:
                await page.wait_for_url(lambda u: all(m not in u for m in UNAUTH_MARKERS), timeout=25000)
            except Exception:
                log(f"login: still on {page.url} after wait_for_url ({time.monotonic() - t0:.1f}s in)")
            await page.wait_for_timeout(2500)

            if any(m in page.url for m in UNAUTH_MARKERS):
                raise KBAuthError(
                    "Automatic login did not complete (likely 2FA, captcha, or SSO). "
                    "Run `weeek-mcp-login` to sign in manually and cache the session."
                )

            cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(cfg.storage_state_path))
            log(f"login: succeeded in {time.monotonic() - t0:.1f}s")
        finally:
            await browser.close()
