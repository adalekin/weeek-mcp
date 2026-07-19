"""Weeek web login via Playwright — used only to (re)acquire the session cookies
that the internal KB API needs. Knowledge base data itself is fetched with httpx.

Weeek's login is a two-step flow: enter email -> Continue -> enter password -> submit.
The saved storageState (cookies) is then reused by the httpx client.
"""

from __future__ import annotations

import json

from ..config import Config

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
    interactive ``weeek-mcp-login`` seeder.
    """
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
            await page.goto(cfg.app_base + LOGIN_PATH, wait_until="domcontentloaded")
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
                pass
            await page.wait_for_timeout(2500)

            if any(m in page.url for m in UNAUTH_MARKERS):
                raise KBAuthError(
                    "Automatic login did not complete (likely 2FA, captcha, or SSO). "
                    "Run `weeek-mcp-login` to sign in manually and cache the session."
                )

            cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(cfg.storage_state_path))
        finally:
            await browser.close()


_EDITOR_SELECTOR = ".ProseMirror, [contenteditable='true']"
_PASTE_JS = """(html) => {
    const el = document.querySelector(".ProseMirror") || document.querySelector("[contenteditable='true']");
    if (!el) return false;
    el.focus();
    const dt = new DataTransfer();
    dt.setData('text/html', html);
    dt.setData('text/plain', el.innerText || '');
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    return true;
}"""


async def replace_article_content(cfg: Config, workspace_id: str, article_id: str, html: str) -> None:
    """Replace a KB article's body in place by driving Weeek's own editor.

    Weeek persists document bodies through a collaborative websocket, not REST, so
    we open the document headlessly, clear it, and paste new HTML (which ProseMirror
    parses into its schema and syncs to the server). The document id is preserved.
    """
    from playwright.async_api import async_playwright

    if not cfg.storage_state_path.exists():
        raise KBAuthError("No saved session. Run `weeek-mcp-login` first.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(storage_state=str(cfg.storage_state_path))
        page = await context.new_page()
        try:
            await page.goto(f"{cfg.app_base}/ws/{workspace_id}/kb/{article_id}", wait_until="domcontentloaded")
            if "/login" in page.url or "/welcome" in page.url:
                raise KBAuthError("Session expired. Run `weeek-mcp-login` to refresh.")
            # Wait for the editor and its collaborative websocket to connect.
            editor = await page.wait_for_selector(_EDITOR_SELECTOR, timeout=20000)
            await page.wait_for_timeout(5000)
            if editor is None:
                raise KBAuthError("Could not locate the document editor.")
            await editor.click()
            await page.wait_for_timeout(200)
            # Select-all + delete (Mod-A is Meta on macOS, Control elsewhere).
            await page.keyboard.press("Meta+A")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(300)

            ok = await page.evaluate(_PASTE_JS, html)
            if not ok:
                raise KBAuthError("Could not locate the document editor.")
            # Give the collaborative sync time to persist server-side.
            await page.wait_for_timeout(5000)
        finally:
            await browser.close()
