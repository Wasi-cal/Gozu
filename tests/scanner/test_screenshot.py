from unittest.mock import AsyncMock, MagicMock, patch

import scanner.screenshot as screenshot_module
from core.models import Finding, Severity
from scanner.screenshot import FindingExtraction, capture_finding_screenshot


def make_finding() -> Finding:
    return Finding(
        key="ABC-1",
        title="Vulnerability [S1]: some message (file.py:1)",
        severity=Severity.HIGH,
        component="proj:file.py",
        line=1,
        message="some message",
        finding_type="vulnerability",
        deep_link="http://localhost:9090/project/issues?id=proj",
        source_tool="sonarqube",
    )


def make_source_locator(inner_text_result=None, inner_text_side_effect=None):
    """A fake Playwright Locator for the matched source-viewer element."""
    loc = MagicMock()
    loc.first = loc
    loc.wait_for = AsyncMock()
    loc.screenshot = AsyncMock()
    if inner_text_side_effect is not None:
        loc.inner_text = AsyncMock(side_effect=inner_text_side_effect)
    else:
        loc.inner_text = AsyncMock(return_value=inner_text_result)
    return loc


def make_header_locator(header_texts):
    """Fake `page.locator("header")` result: `.count()` + `.nth(i).inner_text()`."""
    headers = MagicMock()
    headers.count = AsyncMock(return_value=len(header_texts))

    def nth(i):
        h = MagicMock()
        h.inner_text = AsyncMock(return_value=header_texts[i])
        return h

    headers.nth = MagicMock(side_effect=nth)
    return headers


def make_page(source_locator, header_texts=()):
    page = MagicMock()
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.screenshot = AsyncMock()

    def locator(selector):
        if selector == "header":
            return make_header_locator(header_texts)
        return source_locator

    page.locator = MagicMock(side_effect=locator)
    return page


def patch_context(page):
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    return patch.object(screenshot_module, "_get_context", AsyncMock(return_value=context))


async def test_capture_finding_screenshot_extracts_code_snippet(tmp_path):
    locator = make_source_locator(inner_text_result="print('hi')")
    page = make_page(locator)
    out_path = tmp_path / "out.png"

    with patch_context(page):
        result = await capture_finding_screenshot(make_finding(), out_path, "test-token")

    assert isinstance(result, FindingExtraction)
    assert result.screenshot_path == out_path
    assert result.code_snippet == "print('hi')"
    locator.inner_text.assert_awaited_once()


async def test_capture_finding_screenshot_handles_inner_text_failure(tmp_path):
    locator = make_source_locator(inner_text_side_effect=Exception("boom"))
    page = make_page(locator)
    out_path = tmp_path / "out.png"

    with patch_context(page):
        result = await capture_finding_screenshot(make_finding(), out_path, "test-token")

    assert result.screenshot_path == out_path
    assert result.code_snippet is None


async def test_capture_finding_screenshot_annotation_text_none_when_no_header_present(tmp_path):
    locator = make_source_locator(inner_text_result="code")
    page = make_page(locator, header_texts=())
    out_path = tmp_path / "out.png"

    with patch_context(page):
        result = await capture_finding_screenshot(make_finding(), out_path, "test-token")

    assert result.annotation_text is None
    assert result.code_snippet == "code"


async def test_capture_finding_screenshot_annotation_text_captured(tmp_path):
    locator = make_source_locator(inner_text_result="code")
    # First header is the page-level nav; the last one is the issue-detail
    # header, whose first line is the annotation message.
    page = make_page(
        locator,
        header_texts=["Sonar to Jira\nIssues", "Fix this issue.\nMore metadata\nOther stuff"],
    )
    out_path = tmp_path / "out.png"

    with patch_context(page):
        result = await capture_finding_screenshot(make_finding(), out_path, "test-token")

    assert result.annotation_text == "Fix this issue."
    assert result.code_snippet == "code"


async def test_capture_finding_screenshot_annotation_extraction_failure_returns_none(tmp_path):
    locator = make_source_locator(inner_text_result="code")

    def locator_fn(selector):
        if selector == "header":
            raise RuntimeError("boom")
        return locator

    page = make_page(locator)
    page.locator = MagicMock(side_effect=locator_fn)
    out_path = tmp_path / "out.png"

    with patch_context(page):
        result = await capture_finding_screenshot(make_finding(), out_path, "test-token")

    assert result.annotation_text is None
    assert result.code_snippet == "code"
    assert result.screenshot_path == out_path
