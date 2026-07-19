"""Knowledge base access for Weeek via Playwright.

Weeek has no public API for the knowledge base, so this package drives the web app.
All DOM-specific details live in ``selectors.py`` and are marked as requiring
validation against the live UI (they cannot be verified without a real login).
"""
