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
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from ..config import Config
from ..logging_util import make_logger

# Asks the server whether it has the edit, given the column widths that were
# applied (None when the edit set none). Supplied by the caller, which is the
# side that knows what the document is supposed to look like.
Settled = Callable[[list[list[int] | None] | None], Awaitable[bool]]

_LOGIN_TIMEOUT = 45.0  # hard ceiling so a stuck browser fails loudly instead of hanging
_EDIT_TIMEOUT = 60.0  # a paste that also restores table widths waits on two syncs
_SETTLE_POLL_MS = 500  # how often the server is asked whether the sync arrived
_APPLY_ATTEMPTS = 2  # a late pass can undo the attribute; set it again, but the whole call
# has to stay inside the caller's 60s and a browser start already costs 15 of them
_APPLY_RECHECK_MS = 1200  # long enough for that pass to have happened
_APPLY_RETRY_MS = 400  # breathing room before setting it once more
_SCROLL_SETTLE_MS = 250  # let the editor finish reacting to the viewport move
_DRAG_SETTLE_MS = 600  # let the plugin write the attribute after a handle is dropped
_SETTLE_TIMEOUT = 30.0  # the wait has to end inside the CALLER's timeout, not just ours: a
# browser start costs ~15s and the transaction ~4s, so anything longer than this gets the whole
# call killed from outside and the diagnosis never reaches whoever asked for the write

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


class KBEditDiscardedError(RuntimeError):
    """The editor took the transaction and then undid it.

    Nothing reaches the collaborative channel in that case, so waiting on the
    server would only spend the caller's timeout to reach the same conclusion.
    """


class KBNotSettledError(RuntimeError):
    """The edit was made in the editor but the server never served it back.

    Weeek syncs the editor over a collaborative websocket. When another live
    session holds the same document — a browser tab left open on it, or a stuck
    headless page from an earlier call — that session keeps overwriting what we
    type, and the document silently stays as it was. Nothing in the editor says
    so, which is why the server is polled instead of trusted.
    """


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

# Both of the operations below need the live EditorView, which Weeek exposes only
# as a Vue prop — no global, no handle on the DOM node. The walk below matches on
# shape (an object with `.view` and `.schema`) and on which DOM element that view
# owns, rather than on component names, so a reshuffled tree still resolves and a
# page with two editors (a task's description and its comment box) cannot hand
# back the wrong one.
_FIND_EDITOR_JS = """
    const isEditor = (o) => {
        try { return o && typeof o === 'object' && o.view && o.schema && o.view.state && o.view.dom; }
        catch (e) { return false; }
    };
    const findEditor = (selector) => {
        const target = document.querySelector(selector);
        if (!target) return null;
        const owns = (ed) => ed.view.dom === target || target.contains(ed.view.dom) || ed.view.dom.contains(target);
        const seen = new WeakSet();
        let found = null;
        const visit = (inst, depth) => {
            if (!inst || found || depth > 60 || seen.has(inst)) return;
            seen.add(inst);
            for (const bagName of ['props', 'setupState', 'data', 'ctx']) {
                const bag = inst[bagName];
                if (!bag || typeof bag !== 'object') continue;
                let keys = [];
                try { keys = Object.keys(bag); } catch (e) { continue; }
                for (const k of keys) {
                    let v;
                    try { v = bag[k]; } catch (e) { continue; }
                    const raw = (v && typeof v === 'object' && v.__v_isRef) ? v.value : v;
                    if (isEditor(raw) && owns(raw)) { found = raw; return; }
                }
            }
            const walk = (vnode, d) => {
                if (!vnode || typeof vnode !== 'object' || d > 40 || found) return;
                if (vnode.component) visit(vnode.component, depth + 1);
                if (Array.isArray(vnode.children)) vnode.children.forEach(c => walk(c, d + 1));
                if (vnode.dynamicChildren) vnode.dynamicChildren.forEach(c => walk(c, d + 1));
                if (vnode.suspense) walk(vnode.suspense.activeBranch, d + 1);
            };
            walk(inst.subTree, 0);
        };
        for (const el of document.querySelectorAll('*')) {
            const inst = (el.__vue_app__ && el.__vue_app__._instance) || (el._vnode && el._vnode.component);
            if (!inst) continue;
            visit(inst, 0);
            if (found) return found;
        }
        return null;
    };
"""

# Selecting everything and pressing Delete does not empty a document that
# contains a table: the table survives the keypress, so the pasted copy lands
# *next to* the old one. Deleting the whole range as a transaction does what the
# keypress only looked like it was doing, and `pasteHTML` then parses the markup
# through the editor's own clipboard parser.
_REPLACE_CONTENT_JS = (
    """({selector, html}) => {"""
    + _FIND_EDITOR_JS
    + """
    const target = document.querySelector(selector);
    // Reported even when the editor cannot be reached: it decides whether the
    // caller may fall back to the clipboard route, which cannot clear a table.
    const hasTables = !!(target && target.querySelector('table'));
    const editor = findEditor(selector);
    if (!editor) return {ok: false, error: 'editor instance not found', hasTables};
    const view = editor.view;
    if (html && typeof view.pasteHTML !== 'function') return {ok: false, error: 'pasteHTML unavailable', hasTables};
    view.dispatch(view.state.tr.delete(0, view.state.doc.content.size));
    if (html) view.pasteHTML(html);
    return {ok: true, hasTables, size: view.state.doc.content.size};
}"""
)

# Column widths are a node attribute the editor's HTML parser ignores (its
# table_body spec parses a bare `tbody`), so they cannot ride in with a paste.
# They have to be set the way the editor's own resizer sets them: a transaction
# on the live EditorView, which then syncs over the collaborative websocket.
#
# This runs in two steps — report what is there, then write what Python worked
# out — so that no width arithmetic happens in the browser, where it could not
# be tested, and so that a document edited between the two steps is caught.
_MEASURE_TABLES_JS = (
    """({selector, minWidth, fallbackWidth}) => {"""
    + _FIND_EDITOR_JS
    + """
    const editor = findEditor(selector);
    if (!editor) return {ok: false, error: 'editor instance not found'};
    const view = editor.view;

    const columnCount = (body) => {
        const row = body.firstChild;
        if (!row) return 0;
        let n = 0;
        row.forEach(cell => { n += (cell.attrs && cell.attrs.colspan) || 1; });
        return n;
    };

    const tables = [];
    view.state.doc.descendants((node) => {
        if (node.type.name === 'table_body') {
            tables.push({columns: columnCount(node), raw_columns: node.attrs.columns || null});
        }
    });

    // What each column would need in order not to wrap, measured in the real
    // font: a clone of every cell is laid out off-screen with nowrap, and the
    // widest one wins. Guessing this from character counts is not good enough —
    // bold, links and emoji are all wider than a plain glyph.
    const rendered = view.dom.querySelectorAll('table');
    const probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;left:-99999px;top:0;white-space:nowrap;';
    view.dom.parentElement.appendChild(probe);
    rendered.forEach((table, i) => {
        if (!tables[i]) return;
        const natural = [], current = [];
        const rows = table.querySelectorAll('tr');
        rows.forEach(row => {
            Array.from(row.children).forEach((cell, k) => {
                const clone = cell.cloneNode(true);
                clone.style.width = 'auto';
                clone.style.maxWidth = 'none';
                clone.style.whiteSpace = 'nowrap';
                probe.appendChild(clone);
                natural[k] = Math.max(natural[k] || 0, Math.ceil(clone.getBoundingClientRect().width));
                probe.removeChild(clone);
                if (current[k] === undefined) current[k] = Math.round(cell.getBoundingClientRect().width);
            });
        });
        tables[i].natural = natural;
        tables[i].current = current;
        tables[i].wraps = natural.filter((w, k) => w > (current[k] || 0) + 1).length;
    });
    probe.remove();

    // The content column, measured rather than assumed: it is what "fit" means.
    const dom = view.dom;
    const style = getComputedStyle(dom);
    const pad = parseFloat(style.paddingLeft || '0') + parseFloat(style.paddingRight || '0');
    const available = Math.max(minWidth, Math.round((dom.clientWidth || fallbackWidth) - pad));

    // A table may be wider than the text: it overhangs the content column. How
    // much wider is a property of the page, not of the editor, so the ancestor
    // chain is reported and the caller picks the ceiling it wants.
    const ancestors = [];
    for (let el = dom.parentElement, i = 0; el && i < 8; el = el.parentElement, i++) {
        const cs = getComputedStyle(el);
        ancestors.push({
            tag: el.tagName.toLowerCase(),
            cls: (el.className || '').toString().slice(0, 60),
            client: el.clientWidth,
            scroll: el.scrollWidth,
            overflowX: cs.overflowX,
        });
    }
    const page = {viewport: window.innerWidth, editor: dom.clientWidth, ancestors};

    return {ok: true, tables, available, page};
}"""
)

_APPLY_COLUMNS_JS = (
    """({selector, columns}) => {"""
    + _FIND_EDITOR_JS
    + """
    const editor = findEditor(selector);
    if (!editor) return {ok: false, error: 'editor instance not found'};
    const view = editor.view;

    const positions = [];
    view.state.doc.descendants((node, pos) => {
        if (node.type.name === 'table_body') positions.push(pos);
    });
    if (positions.length !== columns.length) {
        return {ok: false, error: 'the document changed while it was being resized'};
    }

    let tr = view.state.tr;
    let changed = 0;
    positions.forEach((pos, i) => {
        if (!columns[i]) return;
        tr = tr.setNodeAttribute(pos, 'columns', JSON.stringify(columns[i]));
        changed++;
    });
    if (changed) view.dispatch(tr);

    // The attribute alone loses: the extension recomputes it from the rendered
    // table, so on a table the editor has already drawn our value is replaced by
    // whatever the DOM says. The resizer works the other way round — it moves the
    // markup and lets the attribute follow. So move the markup too.
    // The colgroup is a ProseMirror widget the plugin redraws from the attribute,
    // so touching it changes nothing that lasts. Reported, not written to.
    const rendered = document.querySelector(selector).querySelectorAll('table');
    const layout = [];
    rendered.forEach((table) => {
        const node = table.closest('.table-node');
        layout.push({
            margin: node ? getComputedStyle(node).marginLeft : null,
            cols: table.querySelectorAll('colgroup col').length,
            widths: Array.from(table.querySelectorAll('colgroup col')).map(c => c.style.width),
        });
    });

    // What the editor actually holds once the transaction has been applied.
    // A dispatch that "succeeded" and an attribute that survived it are two
    // different things: if the table extension recomputes columns of its own
    // accord, ours is gone before the sync ever sees it, and the server is
    // right to keep serving the old widths.
    const after = [];
    view.state.doc.descendants((node) => {
        if (node.type.name === 'table_body') after.push(node.attrs.columns || null);
    });
    // Compare the widths, not the text: the plugin rebuilds the same object with
    // its own key order and its own extra fields, and a string comparison then
    // reports a change that landed perfectly well as discarded.
    const widthsOf = (raw) => {
        try { return (JSON.parse(raw) || []).map(e => e && e.width); } catch (e) { return null; }
    };
    const same = (a, b) => a && b && a.length === b.length && a.every((v, k) => v === b[k]);
    const stuck = positions.map((_, i) =>
        !columns[i] ? null : same(widthsOf(after[i]), columns[i].map(c => c.width))
    );
    return {ok: true, tables: positions.length, changed, stuck, after, layout};
}"""
)


# The resize handles are real elements the plugin listens on: `left` gives where
# each column boundary sits. Reported with page coordinates so the mouse can be
# driven to them, which is the one route the plugin treats as authoritative.
# What the editor actually exposes. `editor.commands` is a Proxy and enumerates
# as empty, which is not the same as having no commands — the real map lives on
# the extension manager. Read once, so the table extension can be asked properly
# instead of argued with through its attributes.
_EXTENSIONS_JS = (
    """({selector}) => {"""
    + _FIND_EDITOR_JS
    + """
    const editor = findEditor(selector);
    if (!editor) return {ok: false, error: 'editor instance not found'};
    const mgr = editor.extensionManager || {};
    const commands = Object.keys(mgr.commands || {});
    const extensions = (mgr.extensions || []).map(e => e.name);
    const table = (mgr.extensions || []).filter(e => /table|column|cell|row/i.test(e.name || ''));
    const details = table.map(e => ({
        name: e.name,
        options: Object.keys(e.options || {}),
        attributes: Object.keys((e.config && e.config.addAttributes) ? e.config.addAttributes.call({}) || {} : {}),
        commandsSource: e.config && e.config.addCommands ? String(e.config.addCommands).slice(0, 4000) : null,
    }));
    const plugins = (editor.view && editor.view.state ? editor.view.state.plugins : [])
        .map(p => (p.key || '').toString());
    return {ok: true, commands, extensions, table: details, plugins};
}"""
)


# The document is a Yjs document; ProseMirror is its view. Dispatching a
# transaction leaves it to y-prosemirror to notice what changed, and for an
# attribute on a node whose content is untouched it frequently notices nothing —
# the editor shows the new width, the Y document never hears about it, and the
# server keeps serving the old one, correctly.
#
# So write where the truth is. The y-sync plugin state carries the Y fragment
# the editor is bound to; setting the attribute on the matching Y element there
# both reaches the server and comes back into the editor by itself.
_YJS_COLUMNS_JS = (
    """({selector, columns}) => {"""
    + _FIND_EDITOR_JS
    + """
    const editor = findEditor(selector);
    if (!editor) return {ok: false, error: 'editor instance not found'};
    const view = editor.view;
    const plugin = view.state.plugins.find(p => String(p.key).startsWith('y-sync'));
    if (!plugin) return {ok: false, error: 'no y-sync plugin'};
    const state = plugin.getState(view.state);
    const fragment = state && (state.type || (state.binding && state.binding.type));
    if (!fragment) return {ok: false, error: 'no bound fragment'};

    const bodies = [];
    const walk = (node) => {
        if (!node || typeof node.toArray !== 'function') return;
        if (node.nodeName === 'table_body') bodies.push(node);
        node.toArray().forEach(walk);
    };
    fragment.toArray().forEach(walk);
    if (bodies.length !== columns.length) {
        return {ok: false, error: `${bodies.length} table(s) in the Y document, ${columns.length} planned`};
    }

    const doc = fragment.doc;
    const write = () => {
        columns.forEach((spec, i) => {
            if (spec) bodies[i].setAttribute('columns', JSON.stringify(spec));
        });
    };
    if (doc && typeof doc.transact === 'function') doc.transact(write);
    else write();

    return {ok: true, tables: bodies.length, after: bodies.map(b => b.getAttribute('columns') || null)};
}"""
)


_HANDLES_JS = (
    """({selector, index}) => {"""
    + _FIND_EDITOR_JS
    + """
    const editor = findEditor(selector);
    const commands = editor && editor.commands ? Object.keys(editor.commands) : [];
    const tables = document.querySelector(selector).querySelectorAll('table');
    const table = tables[index];
    if (!table) return {ok: false, error: 'no such table', commands};
    const node = table.closest('.table-node') || table;
    node.scrollIntoView({block: 'center'});
    const handles = Array.from(node.querySelectorAll('.table__widget-columns-resizer')).map(h => {
        const r = h.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2, height: r.height};
    });
    const body = table.getBoundingClientRect();
    return {ok: true, handles, left: body.x, top: body.y, height: body.height, commands};
}"""
)


async def replace_article_content(
    cfg: Config,
    workspace_id: str,
    article_id: str,
    html: str,
    columns_plan: list[dict | None] | None = None,
    settled: Settled | None = None,
) -> None:
    """Replace a KB article's body in place by driving Weeek's own editor.

    ``columns_plan`` restores table column widths right after the replacement, in
    the same browser session — new markup always lands with default widths.
    ``settled`` reports whether the server has the edit; the browser stays open
    until it says yes.
    """
    await _replace_editor_content(
        cfg,
        f"/ws/{workspace_id}/kb/{article_id}",
        html,
        what="document body",
        selector=_KB_EDITOR,
        columns_plan=columns_plan,
        settled=settled,
    )


async def set_table_columns(
    cfg: Config,
    workspace_id: str,
    article_id: str,
    plan: list[dict | None],
    settled: Settled | None = None,
) -> dict:
    """Set column widths on an article's tables without touching their content.

    ``settled`` reports whether the server has the new widths; the page stays
    open until it says yes.
    """
    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(
            _size_tables(cfg, f"/ws/{workspace_id}/kb/{article_id}", plan, settled), timeout=_EDIT_TIMEOUT
        )
    except TimeoutError as exc:
        raise KBAuthError(
            f"Sizing the tables timed out after {_EDIT_TIMEOUT:.0f}s (stuck at {time.monotonic() - t0:.1f}s in)."
        ) from exc


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


async def _replace_editor_content(
    cfg: Config,
    page_path: str,
    html: str,
    *,
    what: str,
    selector: str,
    columns_plan: list[dict | None] | None = None,
    settled: Settled | None = None,
) -> None:
    """Drive one of Weeek's collaborative editors to hold exactly ``html``.

    Bounded by ``_EDIT_TIMEOUT`` so a stuck browser/editor fails with a clear error
    instead of hanging past the MCP client's own tool-call timeout untraced.
    """
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(
            _edit(cfg, page_path, html, what, selector, columns_plan, settled), timeout=_EDIT_TIMEOUT
        )
    except TimeoutError as exc:
        raise KBAuthError(
            f"Editing the {what} timed out after {_EDIT_TIMEOUT:.0f}s (stuck at {time.monotonic() - t0:.1f}s in)."
        ) from exc


async def _clipboard_replace(page, selector: str, html: str, what: str) -> None:
    """Select-all, Delete, paste — the route used before the editor was reachable."""
    if not await page.evaluate(_SELECT_ALL_JS, selector):
        raise KBAuthError(f"Could not locate the editor for the {what}.")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(300)
    if html.strip() and not await page.evaluate(_PASTE_JS, {"selector": selector, "html": html}):
        raise KBAuthError(f"Could not locate the editor for the {what}.")


@asynccontextmanager
async def _editor_page(cfg: Config, page_path: str, selector: str, what: str):
    """Open ``page_path`` headlessly with the saved session, editor ready to drive."""
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
            editor = await page.wait_for_selector(selector, timeout=20000)
            if editor is None:
                raise KBAuthError(f"Could not locate the editor for the {what}.")
            log(f"{what}: editor ready in {time.monotonic() - t0:.1f}s")
            # The editor renders before the collaborative websocket has caught up;
            # acting sooner would edit a document that is not there yet.
            await page.wait_for_timeout(5000)
            yield page, editor, log
        finally:
            await browser.close()


async def _apply_columns(page, selector: str, plan: list[dict | None]) -> dict:
    """Size the tables the plan describes, then let the collaborative sync persist it.

    Measures first and resolves the widths in Python: the browser only writes the
    attribute it is handed, and a document that changed between the two steps is
    refused rather than written to blindly.
    """
    from .tables import FALLBACK_CONTENT_WIDTH, MIN_WIDTH, MeasuredTable, resolve_columns

    measured: dict = await page.evaluate(
        _MEASURE_TABLES_JS,
        {"selector": selector, "minWidth": MIN_WIDTH, "fallbackWidth": FALLBACK_CONTENT_WIDTH},
    )
    if not measured.get("ok"):
        raise KBAuthError(f"Could not read the tables: {measured.get('error', 'unknown reason')}.")

    tables = [MeasuredTable(columns=t["columns"], raw_columns=t.get("raw_columns")) for t in measured["tables"]]
    sizing = [
        {"columns": t["columns"], "natural": t.get("natural"), "current": t.get("current"), "wraps": t.get("wraps")}
        for t in measured["tables"]
    ]
    available = int(measured["available"])
    columns = resolve_columns(tables, plan, available)

    # The editor keeps working on the document after it first renders, and a late
    # pass puts back the attributes of tables it re-reconciles — which is why the
    # tables near the top of a long document were the ones that never kept their
    # widths. Dispatching once and hoping is not enough: set, look, set again.
    applied: dict = {}
    settled_in_editor: dict = {}
    for attempt in range(_APPLY_ATTEMPTS):
        # The tables the editor is looking at are the ones it keeps rewriting, and
        # at the top of a long document that is always the same few. Moving the
        # viewport between attempts gives every table a turn while it is off
        # screen, which is the only state in which the attribute has ever stuck.
        await page.evaluate(
            "(toBottom) => window.scrollTo(0, toBottom ? document.body.scrollHeight : 0)",
            bool(attempt % 2),
        )
        await page.wait_for_timeout(_SCROLL_SETTLE_MS)
        applied = await page.evaluate(_APPLY_COLUMNS_JS, {"selector": selector, "columns": columns})
        if not applied.get("ok"):
            raise KBAuthError(f"Could not set table widths: {applied.get('error', 'unknown reason')}.")
        await page.wait_for_timeout(_APPLY_RECHECK_MS)
        settled_in_editor = await page.evaluate(
            _APPLY_COLUMNS_JS, {"selector": selector, "columns": [None] * len(columns)}
        )
        held = [
            after == json.dumps(columns[i])
            for i, after in enumerate(settled_in_editor.get("after") or [])
            if columns[i]
        ]
        if all(held):
            break
        if attempt + 1 < _APPLY_ATTEMPTS:
            await page.wait_for_timeout(_APPLY_RETRY_MS)
    applied["stuck"] = [
        None if not columns[i] else (after == json.dumps(columns[i]))
        for i, after in enumerate(settled_in_editor.get("after") or [None] * len(columns))
    ]
    return {
        "tables": applied["tables"],
        "changed": applied["changed"],
        "resolved": columns,
        "stuck": applied.get("stuck"),
        "layout": applied.get("layout"),
        "editor_after": settled_in_editor.get("after"),
        "available": available,
        "page": measured.get("page"),
        "sizing": sizing,
        "expected": [[c["width"] for c in cols] if cols else None for cols in columns],
    }


async def _drag_columns(page, selector: str, index: int, widths: list[int], log) -> dict:
    """Resize a table the way a person does: by dragging its handles.

    Writing the attribute is the wrong end of the lever — the plugin owns it and
    recomputes it from the layout for any table it has already laid out. The
    handles are what the plugin itself listens on, so a real drag is the only
    instruction it treats as coming from the document's owner.
    """
    report = await page.evaluate(_HANDLES_JS, {"selector": selector, "index": index})
    if not report.get("ok"):
        return {"dragged": 0, "error": report.get("error"), "commands": report.get("commands")}
    await page.wait_for_timeout(400)

    dragged = 0
    for k in range(len(widths) - 1):
        report = await page.evaluate(_HANDLES_JS, {"selector": selector, "index": index})
        handles = report.get("handles") or []
        if k >= len(handles):
            break
        handle = handles[k]
        target = report["left"] + sum(widths[: k + 1])
        if abs(handle["x"] - target) < 2:
            continue
        await page.mouse.move(handle["x"], handle["y"])
        await page.mouse.down()
        # A couple of intermediate moves: a single jump can miss a handler that
        # only starts tracking after the pointer has actually moved.
        await page.mouse.move((handle["x"] + target) / 2, handle["y"], steps=4)
        await page.mouse.move(target, handle["y"], steps=4)
        await page.mouse.up()
        await page.wait_for_timeout(_DRAG_SETTLE_MS)
        dragged += 1
    log(f"drag columns: table {index}, {dragged} handle(s) moved")
    return {"dragged": dragged, "commands": report.get("commands")}


async def _size_tables(cfg: Config, page_path: str, plan: list[dict | None], settled: Settled | None = None) -> dict:
    """Open the article headlessly and set column widths, leaving content alone.

    The widths ride the same collaborative websocket as a body edit, so closing
    the page before the sync lands throws them away — and on a long document
    that is what happens every time, silently. ``settled`` asks the server.
    """
    t0 = time.monotonic()
    async with _editor_page(cfg, page_path, _KB_EDITOR, "size tables") as (page, _editor, log):
        opened = time.monotonic() - t0
        # Read the editor's own surface first: a failure further down must not be
        # the reason this never gets reported. That has happened twice already.
        editor_api = await page.evaluate(_EXTENSIONS_JS, {"selector": _KB_EDITOR})
        result = await _apply_columns(page, _KB_EDITOR, plan)
        # No direct write into the Y document. Reaching into it behind
        # y-prosemirror desynchronises the binding, and after that nothing the
        # editor does reaches the server at all — tables that had been writing
        # fine all evening stopped the moment it was added.
        applied = time.monotonic() - t0
        log(f"size tables: {result['changed']}/{result['tables']} in {applied:.1f}s")
        # No dragging: the handles are only in the markup once a pointer has been
        # over the table, so a headless page never has any. And no raising on the
        # in-editor check either — it has been wrong in both directions today, and
        # the server's answer below is the one that decides.
        drags = []
        # Wait for the server, now that the attribute demonstrably holds: the
        # repeated set is what made it hold, and without a wait the page closes
        # before the sync leaves it. An earlier version of this waited without
        # the repeats, watched an attribute that had already been put back, and
        # was blamed for the failure it was only reporting.
        waited = {}
        if settled is not None:
            try:
                waited = await _wait_until_settled(page, settled, result["expected"], "table widths", log)
            except KBNotSettledError as exc:
                raise KBNotSettledError(f"{exc} Yjs write: {result.get('yjs')}.") from exc
        # Where the time goes, reported to the caller: the browser start is a
        # fixed cost, and what is left of the client's timeout is the budget the
        # sync has to land in. Without these numbers a failure says nothing.
        result["drags"] = drags or None
        result["editor_api"] = editor_api
        result["after_drag"] = result.get("after_drag")
        result["timings"] = {
            "open_s": round(opened, 1),
            "apply_s": round(applied - opened, 1),
            "total_s": round(time.monotonic() - t0, 1),
            **waited,
        }
        return result


async def _edit(
    cfg: Config,
    page_path: str,
    html: str,
    what: str,
    selector: str,
    columns_plan: list[dict | None] | None = None,
    settled: Settled | None = None,
) -> None:
    """Open the page headlessly and make the editor ``selector`` points at hold ``html``.

    Weeek persists these bodies through a collaborative websocket, not REST, so
    ProseMirror has to parse the HTML and sync it itself. Ids are preserved.
    The replacement runs as an editor transaction, falling back to the clipboard
    route only if the editor instance cannot be reached — that fallback cannot
    clear tables (see ``_REPLACE_CONTENT_JS``), so it is a last resort.

    Closing the browser before the sync has reached the server throws the edit
    away, and the editor gives no sign of it: ``settled`` asks the server itself,
    and the page is held open until it answers yes.
    """
    t0 = time.monotonic()
    async with _editor_page(cfg, page_path, selector, f"edit {what}") as (page, editor, log):
        await editor.click()
        await page.wait_for_timeout(200)

        replaced = await page.evaluate(_REPLACE_CONTENT_JS, {"selector": selector, "html": html.strip()})
        if not replaced.get("ok"):
            if replaced.get("hasTables") or "<table" in html.lower():
                # The clipboard route cannot clear a table, so it would leave the old
                # one sitting next to the new content. Refusing beats writing that.
                raise KBAuthError(
                    f"Could not reach Weeek's editor to replace the {what} "
                    f"({replaced.get('error', 'unknown reason')}), and the fallback route "
                    "cannot replace tables without duplicating them, so nothing was written."
                )
            log(f"edit {what}: transaction route unavailable ({replaced.get('error')}), using the clipboard")
            await _clipboard_replace(page, selector, html, what)
        widths: list[list[int] | None] | None = None
        if columns_plan:
            # Both transactions travel the same channel, so they are dispatched
            # together and waited on together.
            sized = await _apply_columns(page, selector, columns_plan)
            widths = sized["expected"]
            log(f"edit {what}: sized {sized['changed']}/{sized['tables']} table(s)")
        if settled is None:
            # Give the collaborative sync time to persist server-side.
            await page.wait_for_timeout(5000)
        else:
            await _wait_until_settled(page, settled, widths, what, log)
        log(f"edit {what}: done in {time.monotonic() - t0:.1f}s")


async def _wait_until_settled(page, settled: Settled, widths, what: str, log) -> dict:
    """Hold the page open until the server reports the edit, or the caller times out.

    The editor confirms nothing: a document that never reached the server reads
    exactly like one that did. Polling the server is the only honest signal, and
    the outer ``_EDIT_TIMEOUT`` bounds the wait.
    """
    t0 = time.monotonic()
    polls = 0
    while not await settled(widths):
        polls += 1
        waited = time.monotonic() - t0
        if waited > _SETTLE_TIMEOUT:
            log(f"edit {what}: server never took the edit ({waited:.1f}s)")
            raise KBNotSettledError(
                f"The {what} was written in the editor but Weeek still serves the previous version "
                f"after {waited:.0f}s and {polls} polls. Nothing was saved. The edit reached the "
                "editor, so this is the collaborative sync not carrying it: the document may be held "
                "by another live session, or this particular change may be one the server drops. "
                "Re-read before doing anything else — and do not retry blindly, a repeat while the "
                "document is held can leave it empty."
            )
        await page.wait_for_timeout(_SETTLE_POLL_MS)
    waited = time.monotonic() - t0
    log(f"edit {what}: server has it after {waited:.1f}s")
    return {"settle_s": round(waited, 1), "polls": polls}
