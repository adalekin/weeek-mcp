"""Weeek's browser channel: login and read-only layout measurements.

Data is fetched with httpx, and bodies are written over the collaborative
channel (see ``collab.py``). Weeek's login is a two-step flow:
enter email -> Continue -> enter password -> submit. The saved storageState
(cookies) is then reused by the httpx client.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TypedDict, cast

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

_TABLES_JS = """(expected, measure) => {
    const editors = Array.from(document.querySelectorAll('.ProseMirror'))
        .filter(editor => editor.querySelector('table'));
    if (editors.length !== 1) return null;
    const editor = editors[0];
    const tables = Array.from(editor.querySelectorAll('table'));
    if (tables.length !== expected.length) return null;
    for (const [i, table] of tables.entries()) {
        if (expected[i] === null) continue;
        const rows = Array.from(table.rows, row => Array.from(row.cells, cell => cell.textContent));
        if (JSON.stringify(rows) !== JSON.stringify(expected[i])) return null;
        if (Array.from(table.rows).some(row => Array.from(row.cells)
            .some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1))) return null;
    }
    if (!measure) return true;
    const documentView = editor.closest('.kb-document-view');
    if (!documentView) return null;
    const inlinePadding = el => {
        const style = getComputedStyle(el);
        return parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    };
    // Weeek's wide-table decoration sets the maximum node span to the editor
    // container width less 96 px. The editor and wrappers consume more of that span.
    const wideSpan = documentView.getBoundingClientRect().width - 96 - inlinePadding(editor);
    const probe = editor.cloneNode(false);
    probe.removeAttribute('id');
    probe.setAttribute('contenteditable', 'false');
    probe.setAttribute('aria-hidden', 'true');
    probe.style.cssText = 'position:fixed;left:-100000px;top:0;visibility:hidden;' +
        'pointer-events:none;width:max-content;max-width:none;';
    editor.after(probe);
    try {
        return tables.map((table, i) => {
            if (expected[i] === null) return null;
            const node = table.closest('.table-node');
            const scroller = table.closest('.table__content');
            if (!node || !scroller) return null;
            const page = Math.floor(wideSpan - inlinePadding(node) - inlinePadding(scroller));
            const copy = table.cloneNode(true);
            for (const group of copy.querySelectorAll('colgroup')) group.remove();
            for (const el of [copy, ...copy.querySelectorAll('th, td, th *, td *')]) {
                el.style.setProperty('white-space', 'nowrap', 'important');
                el.style.setProperty('max-width', 'none', 'important');
                el.style.setProperty('min-width', '0', 'important');
                el.style.setProperty('width', 'auto', 'important');
            }
            copy.style.setProperty('width', 'max-content', 'important');
            copy.style.setProperty('table-layout', 'auto', 'important');
            probe.append(copy);
            const widths = Array.from(copy.rows[0].cells, cell => Math.ceil(cell.getBoundingClientRect().width));
            copy.remove();
            return {natural: widths, page};
        });
    } finally {
        probe.remove();
    }
}"""


class TableMeasurement(TypedDict):
    natural: list[int]
    page: int


async def measure_table_columns(
    cfg: Config, workspace_id: str, doc_id: str, expected: list[list[list[str]] | None]
) -> list[TableMeasurement | None]:
    """Measure natural widths against the expected cells in the rendered article."""
    try:
        from playwright.async_api import Error, async_playwright
    except ModuleNotFoundError as exc:
        raise KBAuthError("Automatic sizing requires Playwright and its Chromium browser.") from exc

    if not cfg.storage_state_path.exists():
        raise KBAuthError("No saved session. Run `weeek-mcp-login` first.")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=cfg.headless, timeout=10000)
            try:
                context = await browser.new_context(storage_state=str(cfg.storage_state_path))
                page = await context.new_page()
                await page.goto(
                    f"{cfg.app_base}/ws/{workspace_id}/kb/{doc_id}", wait_until="domcontentloaded", timeout=15000
                )
                if any(marker in page.url for marker in UNAUTH_MARKERS):
                    raise KBAuthError("Session expired. Run `weeek-mcp-login` to refresh.")
                await page.wait_for_function(
                    "expected => (" + _TABLES_JS + ")(expected, false)", arg=expected, timeout=25000
                )
                result = await asyncio.wait_for(
                    page.evaluate(
                        "async expected => { await document.fonts.ready; return ("
                        + _TABLES_JS
                        + ")(expected, true); }",
                        expected,
                    ),
                    timeout=5,
                )
            finally:
                await browser.close()
    except (Error, TimeoutError) as exc:
        raise KBAuthError("Could not measure the rendered tables. Check the browser and reload the article.") from exc
    if not isinstance(result, list):
        raise KBAuthError("The rendered tables changed during measurement.")
    return cast(list[TableMeasurement | None], result)


class KBAuthError(RuntimeError):
    """Not logged in and unable to log in (missing credentials, 2FA, captcha, or SSO)."""


def load_cookies_into(client, cfg: Config) -> int:
    """Load storageState cookies into an httpx client. Returns the count loaded."""
    path = cfg.storage_state_path
    if not path.exists():
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
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
