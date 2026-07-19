"""One-time interactive login to seed the Playwright session (storageState).

Run this once (`weeek-mcp-login`). It opens a real browser window, lets you sign
in manually — handling any 2FA or captcha yourself — and then saves the session
so the MCP server can reuse it headlessly.
"""

from __future__ import annotations

import asyncio

from ..config import Config


async def _run() -> None:
    from playwright.async_api import async_playwright

    cfg = Config.from_env()
    print(f"Opening {cfg.app_base} — sign in in the browser window that appears.")
    print("When you can see your workspace, return here and press Enter.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(cfg.app_base + "/login")

        # Block until the user confirms they have logged in.
        await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter once logged in... ")

        cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(cfg.storage_state_path))
        print(f"Session saved to {cfg.storage_state_path}")
        await browser.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
