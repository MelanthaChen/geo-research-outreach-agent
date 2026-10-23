from pathlib import Path


APP = Path(__file__).parents[1] / "src/outreach_agent/dashboard/app.js"


def test_research_queue_keeps_selected_ids_when_filters_change():
    source = APP.read_text(encoding="utf-8")
    search_start = source.find("function applyResearchSearch")
    search_listener = source[search_start:source.find("[\"research-type\"", search_start)]
    filter_listener = source.split('["research-type","research-geography","research-consent"]', 1)[1].split('\n', 1)[0]
    assert "selected.clear()" not in search_listener
    assert "selected.clear()" not in filter_listener
    assert "researchState.selected.size" in source


def test_research_queue_ignores_stale_responses_and_binds_detail_delegation():
    source = APP.read_text(encoding="utf-8")
    assert "requestSequence" in source
    assert "if(requestId!==researchState.requestSequence)return" in source
    assert "researchDetailBound" in source
    assert "loadResearchDetail(button.dataset.researchDetail)" in source


def test_select_all_matching_has_explicit_large_scope_confirmation():
    source = APP.read_text(encoding="utf-8")
    assert "result.total>researchState.pageSize" in source
    assert "This will select ${result.total} matching records across all pages" in source
