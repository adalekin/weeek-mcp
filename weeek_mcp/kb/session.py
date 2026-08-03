"""Weeek's browser channel: login via Playwright, plus the collaborative editors.

Login runs only to (re)acquire the session cookies the internal KB API needs —
data itself is fetched with httpx. Weeek's login is a two-step flow: enter email
-> Continue -> enter password -> submit. The saved storageState (cookies) is then
reused by the httpx client.

The editors are here for the same reason: KB document bodies and task descriptions
are not writable over REST at all, so they are edited by driving Weeek's own UI.
Task descriptions live in the task-manager domain rather than the knowledge base,
but they share this transport, so both live in this module.
"""

from __future__ import annotations

import asyncio
import json
import time

from ..config import Config
from ..logging_util import make_logger

_LOGIN_TIMEOUT = 45.0  # hard ceiling so a stuck browser fails loudly instead of hanging
_EDIT_TIMEOUT = 45.0

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
        raise KBAuthError(f"Login timed out after {_LOGIN_TIMEOUT:.0f}s (stuck at {time.monotonic() - t0:.1f}s in).") from exc


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


_KB_EDITOR = ".ProseMirror, [contenteditable='true']"  # a KB page has exactly one editor
# A task page has two: the description and the comment box below it. Anchor on the
# description wrapper — picking "the first one" would one day clear a comment.
_TASK_DESCRIPTION_EDITOR = ".description .ProseMirror"

# Select-all via the Selection API rather than a keyboard shortcut: on macOS
# Control+A is "move to line start" inside ProseMirror, which quietly turns the
# following Delete into a one-character edit.
_SELECT_ALL_JS = """(selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
}"""
_PASTE_JS = """({selector, html}) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    el.focus();
    const dt = new DataTransfer();
    dt.setData('text/html', html);
    dt.setData('text/plain', el.innerText || '');
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    return true;
}"""


async def replace_article_content(cfg: Config, workspace_id: str, article_id: str, html: str) -> None:
    """Replace a KB article's body in place by driving Weeek's own editor."""
    await _replace_editor_content(
        cfg, f"/ws/{workspace_id}/kb/{article_id}", html, what="document body", selector=_KB_EDITOR
    )


async def replace_task_description(cfg: Config, workspace_id: str, task_id: str, html: str) -> None:
    """Replace a task's description in place by driving Weeek's own editor.

    ``PUT /tm/tasks/{id}`` has no description field (confirmed against Weeek's
    OpenAPI spec — only create does), because descriptions sync through the same
    collaborative channel as KB bodies. Empty html clears the description.
    """
    await _replace_editor_content(
        cfg,
        f"/ws/{workspace_id}/task/{task_id}",
        html,
        what="task description",
        selector=_TASK_DESCRIPTION_EDITOR,
    )


async def _replace_editor_content(cfg: Config, page_path: str, html: str, *, what: str, selector: str) -> None:
    """Drive one of Weeek's collaborative editors to hold exactly ``html``.

    Bounded by ``_EDIT_TIMEOUT`` so a stuck browser/editor fails with a clear error
    instead of hanging past the MCP client's own tool-call timeout untraced.
    """
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(_edit(cfg, page_path, html, what, selector), timeout=_EDIT_TIMEOUT)
    except TimeoutError as exc:
        raise KBAuthError(
            f"Editing the {what} timed out after {_EDIT_TIMEOUT:.0f}s (stuck at {time.monotonic() - t0:.1f}s in)."
        ) from exc


async def _edit(cfg: Config, page_path: str, html: str, what: str, selector: str) -> None:
    """Open the page headlessly, clear the editor ``selector`` points at, paste new HTML.

    Weeek persists these bodies through a collaborative websocket, not REST, so
    ProseMirror has to parse the HTML and sync it itself. Ids are preserved.
    """
    from playwright.async_api import async_playwright

    if not cfg.storage_state_path.exists():
        raise KBAuthError("No saved session. Run `weeek-mcp-login` first.")

    log = make_logger(cfg.log_path, "weeek-mcp/kb")
    t0 = time.monotonic()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(storage_state=str(cfg.storage_state_path))
        page = await context.new_page()
        try:
            await page.goto(cfg.app_base + page_path, wait_until="domcontentloaded", timeout=15000)
            if "/login" in page.url or "/welcome" in page.url:
                raise KBAuthError("Session expired. Run `weeek-mcp-login` to refresh.")
            # Wait for the editor and its collaborative websocket to connect.
            editor = await page.wait_for_selector(selector, timeout=20000)
            log(f"edit {what}: editor ready in {time.monotonic() - t0:.1f}s")
            await page.wait_for_timeout(5000)
            if editor is None:
                raise KBAuthError(f"Could not locate the editor for the {what}.")
            await editor.click()
            await page.wait_for_timeout(200)
            if not await page.evaluate(_SELECT_ALL_JS, selector):
                raise KBAuthError(f"Could not locate the editor for the {what}.")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(300)

            if html.strip():
                ok = await page.evaluate(_PASTE_JS, {"selector": selector, "html": html})
                if not ok:
                    raise KBAuthError(f"Could not locate the editor for the {what}.")
            # Give the collaborative sync time to persist server-side.
            await page.wait_for_timeout(5000)
            log(f"edit {what}: done in {time.monotonic() - t0:.1f}s")
        finally:
            await browser.close()
