"""Playwright automation against the Nova Scotia UARB filing portal.

The UARB site runs FileMaker WebDirect, a JavaScript-heavy single page app that
does not respond to plain HTTP requests. Every interaction is therefore driven
through a real Chromium session and every step waits on a specific DOM
condition rather than a fixed sleep. All content lives in the top document, so
no frame locators are required.

The selectors below were validated against the live portal (matter M12205):

* Matter input  - the ``.fm-textarea`` whose ``.placeholder`` reads "eg M01234".
* Search button - the last ``button.fm-widget`` whose text is "Search".
* Matter header - ``.v-panel-content`` on the detail page.
* Document tabs - ``button.fm-widget .fm-button-bar-segment-label`` whose text is
  "<Name> - <count>" (e.g. "Exhibits - 13").
* Document rows - one "GO GET IT" ``button.fm-widget`` per visible row.
* Metadata      - ``.fm-textarea.v-readonly .inner_border .text`` indices 0-6 hold
  the matter header values; indices 7+ belong to document rows.

The Exhibits list is paginated (9 of 13 rows render initially), so the scraper
scrolls the last visible row into view to lazy-load additional rows until enough
are present or no further rows load.

Failure taxonomy:

* MatterNotFoundError  - the matter number returned no results (fatal).
* TabEmptyError        - a requested tab has zero documents (non-fatal).
* DownloadError        - a single file failed to download after retries.
* ScraperSessionError  - the FileMaker session expired or reset mid-run (fatal).
"""

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import (
    Browser,
    Download,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from core import config
from core.logger import get_logger
from core.models import VALID_DOC_TYPES, MatterMetadata

logger = get_logger(__name__)


class ScraperError(Exception):
    """Base class for all scraper failures."""


class MatterNotFoundError(ScraperError):
    """The supplied matter number returned no results. Fatal for the job."""


class TabEmptyError(ScraperError):
    """A requested tab exists but contains zero documents. Non-fatal."""


class DownloadError(ScraperError):
    """A specific document failed to download after all retry attempts."""


class ScraperSessionError(ScraperError):
    """The FileMaker session expired or redirected to home mid-run. Fatal."""


MATTER_PLACEHOLDER = "eg M01234"
SEARCH_TEXT = "Search"
GO_GET_IT_TEXT = "GO GET IT"

SELECTORS = {
    "matter_textarea": ".fm-textarea",
    "placeholder": ".placeholder",
    "editable_text": ".text",
    "search_button": "button.fm-widget",
    "matter_header": ".v-panel-content",
    "tab_label": "button.fm-widget .fm-button-bar-segment-label",
    "go_get_it": "button.fm-widget",
    "metadata_values": ".fm-textarea.v-readonly .inner_border .text",
}

_TAB_COUNT_PATTERN = re.compile(r"^(.*?)\s*[-\u2013]\s*(\d+)\s*$")

_MAX_SCROLL_ATTEMPTS = 25
_SCROLL_WAIT_MS = 1500
_TAB_SWITCH_WAIT_MS = 600


@dataclass
class ScrapeResult:
    """Outcome of a scrape: downloaded file paths per type plus matter metadata."""

    files_by_type: dict[str, list[Path]] = field(default_factory=dict)
    metadata: MatterMetadata | None = None


def _matter_input(page: Page) -> Locator:
    """Locate the 'Go Directly to Matter' input by its placeholder text."""
    return page.locator(SELECTORS["matter_textarea"]).filter(
        has=page.locator(SELECTORS["placeholder"], has_text=MATTER_PLACEHOLDER)
    )


def _go_get_it_buttons(page: Page) -> Locator:
    """Locate every visible 'GO GET IT' download button."""
    return page.locator(SELECTORS["go_get_it"], has_text=GO_GET_IT_TEXT)


def _tab_labels(page: Page) -> Locator:
    """Locate the document tab labels."""
    return page.locator(SELECTORS["tab_label"])


async def _with_retries(factory, *, attempts: int, backoff_s: int, description: str):
    """Run an awaitable factory with bounded retries and linear backoff.

    ``factory`` is a zero-argument callable returning a fresh coroutine on each
    attempt. The last exception is re-raised as a DownloadError when all
    attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "retrying after failure",
                extra={
                    "step": "scraper.retry",
                    "description": description,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "error": str(exc),
                },
            )
            if attempt < attempts:
                await asyncio.sleep(backoff_s)
    raise DownloadError(f"{description} failed after {attempts} attempts: {last_exc}")


async def _assert_session_alive(page: Page) -> None:
    """Raise ScraperSessionError if the matter detail view is no longer present."""
    detail_label = _tab_labels(page).filter(has_text=re.compile(r" - \d+"))
    if await detail_label.count() == 0:
        raise ScraperSessionError("matter detail tabs missing; session may have reset")


async def _navigate_to_matter(page: Page, matter_number: str) -> None:
    """Open the portal, type the matter number, and search for it."""
    await page.goto(config.UARB_BASE_URL, wait_until="domcontentloaded")

    matter_textarea = _matter_input(page)
    try:
        await matter_textarea.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise ScraperSessionError("matter input never became visible") from exc

    await matter_textarea.click()
    await page.wait_for_timeout(300)
    await page.keyboard.press("Control+a")
    await page.keyboard.type(matter_number, delay=50)

    search_button = page.locator(SELECTORS["search_button"], has_text=SEARCH_TEXT).last
    await search_button.click()

    detail_label = _tab_labels(page).filter(has_text=re.compile(r" - \d+"))
    try:
        await detail_label.first.wait_for(
            state="visible", timeout=config.SELECTOR_TIMEOUT_MS
        )
    except PlaywrightTimeoutError as exc:
        raise MatterNotFoundError(f"no results for matter {matter_number}") from exc


async def _read_tab_counts(page: Page) -> dict[str, int]:
    """Parse per-tab document counts from tab labels such as 'Exhibits - 13'."""
    counts: dict[str, int] = {t: 0 for t in VALID_DOC_TYPES}
    labels = _tab_labels(page)
    total = await labels.count()
    for i in range(total):
        text = (await labels.nth(i).inner_text()).strip()
        match = _TAB_COUNT_PATTERN.match(text)
        if not match:
            continue
        name = match.group(1).strip()
        if name in VALID_DOC_TYPES:
            counts[name] = int(match.group(2))
    return counts


async def _metadata_value(page: Page, index: int) -> str:
    """Read a single matter-header metadata value by positional index."""
    values = page.locator(SELECTORS["metadata_values"])
    if await values.count() <= index:
        return ""
    try:
        return (await values.nth(index).inner_text()).strip()
    except Exception:
        return ""


async def _matter_title(page: Page) -> str:
    """Read the matter title from its dedicated v-label.

    The title renders as a ``.v-label-text`` element alongside seven fixed
    field captions that share the same class. The title is identified by
    excluding the known static caption strings and selecting the remaining
    label.
    """
    return await page.evaluate(
        """() => {
            const captions = new Set([
                'Matter NoStatus', 'Title - Description', 'TypeCategory',
                'Date Received', 'Decision Date', 'Date Final Submission',
                'Outcome', 'Status', 'Public Documents Database'
            ]);
            const labels = [...document.querySelectorAll('.v-label-text')];
            let best = '';
            for (const el of labels) {
                const t = el.textContent.trim();
                if (!captions.has(t) && t.length > best.length) best = t;
            }
            return best;
        }"""
    )


async def _extract_metadata(
    page: Page,
    matter_number: str,
    requested_types: list[str],
    tab_counts: dict[str, int],
) -> MatterMetadata:
    """Build a MatterMetadata model from the matter header.

    The title is read from its dedicated v-label. The remaining values come from
    fixed indices of ``.fm-textarea.v-readonly .inner_border .text``:
    0 matter number, 1 type, 2 status, 3 date received, 4 date final
    submissions, 5 outcome, 6 category.
    """
    title = await _matter_title(page)
    matter_type = await _metadata_value(page, 1)
    status = await _metadata_value(page, 2)
    date_received = await _metadata_value(page, 3)
    date_final_submissions = await _metadata_value(page, 4)
    outcome_raw = await _metadata_value(page, 5)
    category = await _metadata_value(page, 6)

    total_per_requested_tab = {t: tab_counts.get(t, 0) for t in requested_types}

    return MatterMetadata(
        matter_number=matter_number,
        title=title,
        matter_type=matter_type,
        status=status,
        category=category,
        date_received=date_received,
        date_final_submissions=date_final_submissions,
        outcome=outcome_raw or None,
        tab_counts=tab_counts,
        requested_types=requested_types,
        downloaded={t: 0 for t in requested_types},
        total_per_requested_tab=total_per_requested_tab,
    )


async def _open_tab(page: Page, doc_type: str) -> None:
    """Click the tab whose label begins with the given document type."""
    label = _tab_labels(page).filter(has_text=re.compile(r"^" + re.escape(doc_type)))
    if await label.count() == 0:
        raise TabEmptyError(f"tab not found for {doc_type}")
    await label.first.click()
    await page.wait_for_timeout(_TAB_SWITCH_WAIT_MS)


async def _grid_row_count(page: Page) -> int:
    """Return the total number of rows the Vaadin Grid reports via aria-rowcount."""
    return await page.evaluate(
        """() => {
            const g = document.querySelector('[role="grid"]');
            if (!g) return 0;
            const n = parseInt(g.getAttribute('aria-rowcount') || '0', 10);
            return Number.isNaN(n) ? 0 : n;
        }"""
    )


async def _download_one(page: Page, button: Locator, dest_path: Path) -> Path:
    """Click a 'GO GET IT' button and save the file from the in-page overlay.

    FileMaker WebDirect opens an overlay div (inside .v-overlay-container) that
    contains a button labelled with the filename. Clicking that button triggers
    the actual browser download. We then close the overlay before continuing.
    """
    _OVERLAY_DOWNLOAD_BTN = ".fm-download-button"
    _OVERLAY_CLOSE_BTN = ".v-window.fm-modal-dialog button.fm-widget"

    async def attempt() -> Path:
        await button.scroll_into_view_if_needed(timeout=config.SELECTOR_TIMEOUT_MS)
        await button.dispatch_event("mousedown")
        await button.dispatch_event("mouseup")
        await button.dispatch_event("click")
        await page.wait_for_timeout(2000)

        overlay_btn = page.locator(_OVERLAY_DOWNLOAD_BTN).first
        await overlay_btn.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT_MS)

        async with page.expect_download(timeout=config.DOWNLOAD_START_TIMEOUT_MS) as dl_info:
            await overlay_btn.click()

        download: Download = await dl_info.value
        target = dest_path / download.suggested_filename
        await download.save_as(target)

        try:
            close_btn = page.locator(".v-window.fm-modal-dialog button", has_text="Close").first
            if await close_btn.is_visible():
                await close_btn.click()
            else:
                await page.keyboard.press("Escape")
        except Exception:
            await page.keyboard.press("Escape")

        await page.locator(".v-window.fm-modal-dialog").wait_for(
            state="hidden", timeout=config.SELECTOR_TIMEOUT_MS
        )

        return target

    return await _with_retries(
        attempt,
        attempts=config.SCRAPER_RETRY_ATTEMPTS,
        backoff_s=config.SCRAPER_RETRY_BACKOFF_S,
        description=f"download in {dest_path.name}",
    )


async def _download_tab(
    page: Page,
    doc_type: str,
    dest_dir: Path,
    expected_count: int,
) -> list[Path]:
    """Open a tab and download up to MAX_DOCUMENTS files into a per-type folder.

    The Vaadin Grid virtualizes rows: it holds only the visible window in the
    DOM and evicts off-screen rows on scroll, so positional indices are not
    stable. Each visible row is therefore identified by its text content. After
    each download the grid is re-queried and, once every visible row has been
    processed, scrolled down to reveal further rows until the requested limit is
    reached or no new rows appear.
    """
    if expected_count <= 0:
        raise TabEmptyError(f"no documents in {doc_type}")

    await _open_tab(page, doc_type)
    await _assert_session_alive(page)

    await page.evaluate(
        "() => { const el = document.querySelector('.v-grid-scroller-vertical'); if (el) el.scrollTop = 0; }"
    )
    await page.wait_for_timeout(_SCROLL_WAIT_MS)

    try:
        await _go_get_it_buttons(page).first.wait_for(
            state="visible", timeout=config.SELECTOR_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        raise TabEmptyError(f"no document rows rendered for {doc_type}")

    total_rows = await _grid_row_count(page)
    limit = min(expected_count, config.MAX_DOCUMENTS, total_rows or expected_count)

    type_dir = dest_dir / doc_type
    type_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    downloaded_keys: set[str] = set()
    stale_scrolls = 0

    while len(saved) < limit and stale_scrolls < _MAX_SCROLL_ATTEMPTS:
        rows = page.locator(".v-grid-body tr")
        row_total = await rows.count()
        downloaded_this_pass = False

        for r in range(row_total):
            row = rows.nth(r)
            key = (await row.inner_text()).strip()
            if not key or key in downloaded_keys:
                continue
            row_button = row.locator(
                SELECTORS["go_get_it"], has_text=GO_GET_IT_TEXT
            )
            if await row_button.count() == 0:
                continue

            await row.scroll_into_view_if_needed(timeout=config.SELECTOR_TIMEOUT_MS)
            path = await _download_one(page, row_button.first, type_dir)
            saved.append(path)
            downloaded_keys.add(key)
            logger.info(
                f"downloaded document {len(saved)}/{limit}: {path.name}",
                extra={"step": "scraper.download", "document_type": doc_type},
            )
            downloaded_this_pass = True
            break

        if len(saved) >= limit:
            break

        if not downloaded_this_pass:
            stale_scrolls += 1
            await page.evaluate(
                "() => { const el = document.querySelector('.v-grid-scroller-vertical'); if (el) el.scrollTop += el.clientHeight; }"
            )
            await page.wait_for_timeout(_SCROLL_WAIT_MS)
        else:
            stale_scrolls = 0

    if not saved:
        raise TabEmptyError(f"no document rows rendered for {doc_type}")

    return saved


async def run(
    matter_number: str,
    document_types: list[str],
    dest_dir: Path,
) -> ScrapeResult:
    """Scrape a matter, downloading the requested document types.

    Opens a single Chromium session, navigates to the matter, extracts metadata
    and tab counts, then downloads each requested type into its own subfolder of
    ``dest_dir``. ``TabEmptyError`` for an individual type is recorded in the
    metadata and does not fail the job; ``MatterNotFoundError`` and
    ``ScraperSessionError`` are fatal and propagate to the caller.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    result = ScrapeResult()
    browser: Browser | None = None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=config.SCRAPER_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-crashpad",
                "--disable-breakpad",
            ],
        )
        try:
            page = await browser.new_page()
            await _navigate_to_matter(page, matter_number)

            tab_counts = await _read_tab_counts(page)
            metadata = await _extract_metadata(
                page, matter_number, document_types, tab_counts
            )
            logger.info(
                "matter loaded",
                extra={
                    "step": "scraper.metadata",
                    "matter_number": matter_number,
                    "tab_counts": tab_counts,
                },
            )

            for doc_type in document_types:
                await _assert_session_alive(page)
                try:
                    saved = await _download_tab(
                        page, doc_type, dest_dir, tab_counts.get(doc_type, 0)
                    )
                    result.files_by_type[doc_type] = saved
                    metadata.downloaded[doc_type] = len(saved)
                except TabEmptyError as exc:
                    logger.info(
                        f"tab empty, continuing: {exc}",
                        extra={"step": "scraper.tab_empty", "document_type": doc_type},
                    )
                    result.files_by_type[doc_type] = []
                    metadata.downloaded[doc_type] = 0

            result.metadata = metadata
            return result
        finally:
            if browser is not None:
                await browser.close()
