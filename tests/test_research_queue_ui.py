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


def test_research_search_reads_latest_dom_value_after_input_event():
    source = APP.read_text(encoding="utf-8")
    assert "function syncResearchSearchFromDom()" in source
    assert 'setTimeout(()=>applyResearchSearch($("research-search").value),0)' in source
    assert "event.target.value" not in source[source.find("function applyResearchSearch"):source.find('["research-type"')]


def test_select_all_matching_has_explicit_large_scope_confirmation():
    source = APP.read_text(encoding="utf-8")
    assert "result.total>researchState.pageSize" in source
    assert "This will select ${result.total} matching records across all pages" in source


def test_small_select_all_is_local_and_does_not_reload_or_call_selection_api():
    source = APP.read_text(encoding="utf-8")
    start = source.index("async function selectAllMatchingResearch()")
    end = source.index("async function generateResearchBatch()", start)
    small_path = source[start:end]
    assert "if(researchState.total<=researchState.pageSize)" in small_path
    assert "selectVisibleResearchRows(true)" in small_path
    assert small_path.index("selectVisibleResearchRows(true)") < small_path.index("apiWrite(\"/api/recruitment/selection\"")
    assert "reloadResearchQueue().catch(showError)" not in small_path.split("const result=", 1)[0]


def test_visible_selection_updates_only_loaded_ids_and_preserves_other_pages():
    source = APP.read_text(encoding="utf-8")
    start = source.index("function selectVisibleResearchRows")
    end = source.index("async function selectAllMatchingResearch", start)
    helper = source[start:end]
    assert "querySelectorAll(\"[data-research-select]\")" in helper
    assert "researchState.selected.add(box.dataset.researchSelect)" in helper
    assert "researchState.selected.delete(box.dataset.researchSelect)" in helper
    assert "researchState.selected=new Set" not in helper
