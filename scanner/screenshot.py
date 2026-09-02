"""
Captures a screenshot of a SonarQube finding's source-viewer panel using
Playwright.

Uses Playwright's async API rather than its sync API (unlike the rest of
this codebase, which calls `requests` synchronously from inside `async def`
Temporal activities): the sync API raises if called from a thread that
already has a running asyncio event loop, which is exactly the situation
inside a Temporal activity coroutine.

Auth is a plain "Authorization: Basic" header set via `extra_http_headers`,
not Playwright's `http_credentials` context option. `http_credentials` only
attaches credentials after the server responds 401 to an unauthenticated
request; SonarQube's web app always serves its SPA shell with a 200 (auth
state is then resolved client-side), so that challenge never happens and
`http_credentials` silently never sends the header at all - confirmed live,
it renders the login page. Forcing the header on every request sidesteps
that entirely.

Not part of the generic ScannerClient contract - it's a SonarQube-specific
bonus capability (Sonar's deep-link URL shape, SONAR_TOKEN auth), called
directly by the screenshot activity rather than through get_scanner_client().
"""

import asyncio
import base64
import os
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from temporalio import activity

from core.models import Finding

_SOURCE_VIEWER_SELECTORS = [
    "table",
    '[data-testid="source-viewer"]',
    ".source-viewer",
]

_playwright = None
_browser = None
_context: BrowserContext | None = None
_init_lock = asyncio.Lock()


async def _get_context() -> BrowserContext:
    global _playwright, _browser, _context

    async with _init_lock:
        if _context is None:
            token = base64.b64encode(f"{os.environ['SONAR_TOKEN']}:".encode()).decode()
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch()
            # A short viewport clips the source-code table before it's fully
            # rendered/scrolled into view, so `table.screenshot()` can capture
            # a mostly-empty region (e.g. just the inline issue annotation,
            # with the flagged code line itself out of frame). A generously
            # tall viewport avoids needing to scroll at all.
            _context = await _browser.new_context(
                extra_http_headers={"Authorization": f"Basic {token}"},
                viewport={"width": 1280, "height": 2000},
            )

    return _context


async def capture_finding_screenshot(finding: Finding, out_path: Path) -> Path:
    context = await _get_context()
    page = await context.new_page()

    try:
        target_url = finding.deep_link + f"&open={finding.key}"
        await page.goto(target_url, wait_until="networkidle", timeout=30000)

        for selector in _SOURCE_VIEWER_SELECTORS:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=5000)
                await locator.screenshot(path=out_path)
                return out_path
            except PlaywrightTimeoutError:
                continue

        activity.logger.warning(
            f"No known source-viewer selector matched for finding {finding.key}; falling back to full-page screenshot"
        )
        await page.screenshot(path=out_path, full_page=True)
        return out_path
    finally:
        await page.close()
