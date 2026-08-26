"""Lightweight source-contract checks for Institution Explorer trust and layout.

The project has no browser-test dependency. These checks keep the two critical UI
invariants visible to pytest: cards require work-linked evidence for every hop, and
the small-screen evidence panel is bounded by the viewport instead of being stacked
into an unscrollable >100vh shell.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "frontend" / "app.js").read_text()
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text()


def test_displayed_suggestions_require_complete_work_linked_path_evidence():
    assert "function hasCompletePathEvidence(result)" in APP_JS
    assert "result?.path_verified === true && steps.length > 0" in APP_JS
    assert (
        "steps.every(step => step?.evidence_verified === true && "
        "publicationEvidenceUrl(step))"
    ) in APP_JS
    assert "const results = allResults.filter(hasCompletePathEvidence);" in APP_JS
    assert "const pathEvidenceComplete = hasCompletePathEvidence(result);" in APP_JS


def test_explorer_results_keep_a_dedicated_scroll_container():
    assert ".explorer-content { min-height:0;" in STYLE_CSS
    assert ".explorer-results-scroll { flex:1 1 auto; min-height:0; overflow-y:auto;" in STYLE_CSS


def test_small_screen_evidence_panel_is_viewport_bounded():
    assert "#author-sidecar:not(.hidden)" in STYLE_CSS
    assert "position:fixed;" in STYLE_CSS
    assert "inset:12px auto 12px 12px;" in STYLE_CSS
    assert "inset:56px 8px 72px;" in STYLE_CSS
    assert "--explorer-window-max:max(44px,min(62vh,calc(100vh" in STYLE_CSS
