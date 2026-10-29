import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from leak.findings import EvidenceRef, Finding, potential_savings
from leak.ledger.merchants import Catalog, Merchant
from leak.share import CardPrivacyError, build_card, inr, render_svg


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from leak.agent.auditor import detect_only
    from leak.config import load_settings
    from leak.extract.run import demo_cache, run_extract
    from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan

    os.environ.pop("OPENROUTER_API_KEY", None)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    s.paths.data_dir = tmp_path_factory.mktemp("share")
    run_scan(s, **demo_inputs())
    run_extract(s, cache=demo_cache(s.llm.extract_model))
    store, _ = open_store(s)
    return s, store, detect_only(store)


def finding(merchant, annual, *, mid=None, type_="duplicate", action="cancel"):
    return Finding(
        type=type_,
        merchant=merchant,
        merchant_id=mid,
        amount_inr=Decimal(annual) / 12,
        annual_impact_inr=Decimal(annual),
        confidence=0.9,
        explanation="Paid for twice; see the statement for account 50100234567890.",
        evidence=[EvidenceRef(evidence_id="pdf:ab12cd34:p1:l14", note="statement line")],
        suggested_action=action,
        account="[ACCT_1]",
    ).with_id()


def test_inr_uses_indian_grouping():
    assert inr(Decimal("24635.4")) == "₹24,635"
    assert inr(1234567) == "₹12,34,567"
    assert inr(999) == "₹999"


def test_demo_card_matches_headline_and_leaks_nothing_personal(demo):
    _, _, findings = demo
    card = build_card(findings)
    assert card.total_inr == potential_savings(findings)
    assert sum(r.annual_inr for r in card.rows) <= card.total_inr
    names = {r.name for r in card.rows}
    assert "Netflix" not in names  # a price hike to review, not part of the savings total
    assert {"Amazon Prime", "JioHotstar"} <= names  # catalog names, parenthetical stripped

    svg = render_svg(card)
    assert inr(card.total_inr) in svg
    for secret in ("Asha", "Testkumar", "[ACCT", "[CARD", "pdf:", "gmail:", "sms:", "<script"):
        assert secret not in svg
    for f in findings:  # model/detector prose never reaches the card
        assert f.explanation not in svg


def test_unknown_merchants_are_anonymous_and_prose_is_dropped():
    fs = [
        finding("Netflix", 7788, mid="netflix"),
        finding("Ramesh Landlord 9876543210", 120000, type_="zombie", action="dispute"),
    ]
    card = build_card(fs)
    assert [r.name for r in card.rows] == ["A subscription", "Netflix"]
    svg = render_svg(card)
    assert "Ramesh" not in svg and "9876543210" not in svg and "50100234567890" not in svg
    assert "1 duplicate" in svg and "1 charge after cancelling" in svg


def test_names_off_hides_catalog_names_too():
    svg = render_svg(build_card([finding("Netflix", 7788, mid="netflix")], names=False))
    assert "Netflix" not in svg and "Paying for it twice" in svg


def test_card_refuses_text_that_looks_personal():
    bad = Catalog([Merchant(id="x", name="Call 9876543210", category="ott")])
    with pytest.raises(CardPrivacyError):
        build_card([finding("x", 1200, mid="x")], catalog=bad)


def test_rows_dedupe_per_merchant_and_cap_with_more():
    fs = [finding(f"M{i}", 1000 * (i + 1), mid=None) for i in range(6)]
    fs.append(finding("M5", 500, type_="trial_conversion"))  # same merchant, smaller: ignored
    card = build_card(fs)
    assert len(card.rows) == 4 and card.more == 2
    assert card.rows[0].annual_inr == 6000


def test_all_clear_card():
    card = build_card([finding("Netflix", 7788, mid="netflix", type_="recurring", action="keep")])
    assert not card.total_inr and not card.rows
    assert "All clear" in render_svg(card)
    assert "nothing leaking" in card.share_text()


def test_share_card_api(demo):
    from leak.api.app import create_app

    s, _, _ = demo
    c = TestClient(create_app(s, demo=True), base_url="http://127.0.0.1")
    j = c.get("/api/share-card").json()
    assert j["text"].startswith("Leak found ₹") and "uvx leak-audit demo" in j["text"]
    r = c.get("/api/share-card.svg?names=false")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "Amazon Prime" not in r.text
