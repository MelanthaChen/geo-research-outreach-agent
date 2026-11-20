from pathlib import Path


def test_outreach_contact_readiness_ui_has_only_evidence_based_summary():
    root = Path(__file__).resolve().parents[1]
    v2 = (root / "src/outreach_v2/static/app.js").read_text()
    legacy_js = (root / "src/outreach_agent/dashboard/app.js").read_text()
    legacy_html = (root / "src/outreach_agent/dashboard/index.html").read_text()
    combined = "\n".join((v2, legacy_js, legacy_html))
    for obsolete in (
        "Verification coverage for completed personalization",
        "Recheck saved first-party contacts",
        "freshly verified",
        "contact_verified",
        "contact_pending_verification",
    ):
        assert obsolete.casefold() not in combined.casefold()
    assert "Eligible for invitation drafting" in v2
    assert "No eligible contact" in v2
    assert "Eligible for invitation drafting" in legacy_js
    assert "No eligible contact" in legacy_js
    assert 'id="research-contact-recovery"' not in legacy_html
    # Repeated table refreshes remove every prior copy before inserting one.
    assert "document.querySelectorAll('#contactReadinessPanel').forEach(panel=>panel.remove())" in v2
    assert v2.count("panel.id='contactReadinessPanel'") == 1

