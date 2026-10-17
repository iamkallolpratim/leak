from datetime import date
from decimal import Decimal

import pytest

from leak.ledger.fx import fx_rate, to_inr
from leak.ledger.merchants import clean_key, default_catalog, resolve_extracted


@pytest.mark.parametrize(
    "text,mid",
    [
        ("UPI-NETFLIX-netflix@hdfcbank-612345678901-AUTOPAY", "netflix"),
        ("UPI-SWIGGY ONE-swiggyone@axisbank-1-MEMBERSHIP", "swiggy_one"),
        ("UPI-SWIGGY-swiggy@ybl-1-ORDER", "swiggy"),
        ("GOOGLE *YOUTUBEPREMIUM MUMBAI IN", "youtube_premium"),
        ("UPI/P2M/1/GOOGLE INDIA DIGITAL/GOOGLE ONE", "google_one"),
        ("IGST ON MARKUP FEE @18%", "gst_on_fee"),
        ("MARKUP FEE ON FOREIGN CURRENCY TXN", "forex_markup"),
        ("DISNEY PLUS HOTSTAR TRIAL MUMBAI IN", "disney_hotstar"),
        ("ACH-DR/CULTFIT HEALTHCARE PVT LTD/CULTPASS ELITE", "cultfit"),
    ],
)
def test_resolve(text, mid):
    assert default_catalog().resolve(text).id == mid


def test_resolve_uses_plan_subject_and_sender():
    cat = default_catalog()
    assert resolve_extracted(cat, "Amazon", "Prime", "", "Welcome").id == "amazon_prime"
    assert resolve_extracted(cat, "Airtel", None, "", "Your Airtel Black bill").id == "airtel_black"
    assert cat.resolve("Your receipt", sender="Netflix <info@account.netflix.com>").id == "netflix"
    assert cat.resolve("NEFT CR-TESTCORP SALARY") is None
    assert clean_key("UPI-RAMESH-[UPI_2]-612345678901-RENT") == "ramesh rent"


def test_bundles_listed():
    assert "disney_hotstar" in default_catalog().get("airtel_black").includes


def test_fx():
    assert fx_rate("USD", date(2025, 8, 1)).inr_per_unit == Decimal("87.5")
    future = fx_rate("USD", date(2027, 1, 1))
    assert future.approximate
    assert to_inr(Decimal(20), "INR", date(2026, 1, 1)) == Decimal("20.00")
    with pytest.raises(KeyError):
        fx_rate("XYZ", date(2025, 1, 1))


@pytest.fixture(scope="module")
def demo_ledger(tmp_path_factory):
    import os

    from leak.config import load_settings
    from leak.extract.run import demo_cache, run_extract
    from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan

    os.environ.pop("GEMINI_API_KEY", None)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    os.environ.pop("GEMINI_API_KEY", None)
    s.paths.data_dir = tmp_path_factory.mktemp("demo")
    run_scan(s, **demo_inputs())
    report = run_extract(s, cache=demo_cache(s.llm.extract_model))
    store, _ = open_store(s)
    return report, store.ledger(), store.events()


def test_demo_runs_offline_for_free(demo_ledger):
    report, _, _ = demo_ledger
    x = report.extract
    assert (x.llm_calls, x.failed_docs, x.cost_usd) == (0, 0, Decimal(0))


def test_three_sources_merge_into_one_entry(demo_ledger):
    _, ledger, _ = demo_ledger
    netflix = [r for r in ledger if r["merchant_id"] == "netflix"]
    assert [r["amount_inr"] for r in netflix] == [649] * 4 + [799] * 2
    assert all(set(r["sources"].split(",")) == {"statement", "sms", "email"} for r in netflix)
    assert all(len(r["evidence"].split(",")) == 3 for r in netflix)


def test_identical_statement_lines_never_merge(demo_ledger):
    _, ledger, _ = demo_ledger
    # Google One is billed on two different accounts: must stay separate entries.
    g1 = [r for r in ledger if r["merchant_id"] == "google_one"]
    assert len({r["account"] for r in g1}) == 2 and len(g1) == 12


def test_forex_fees_linked_and_usd_kept(demo_ledger):
    _, ledger, _ = demo_ledger
    by_id = {r["id"]: r for r in ledger}
    fees = [r for r in ledger if r["merchant_id"] in ("forex_markup", "gst_on_fee")]
    assert len(fees) == 12
    assert all(by_id[f["related_to"]]["merchant_id"] == "chatgpt" for f in fees)
    gpt = [r for r in ledger if r["merchant_id"] == "chatgpt"]
    assert all(r["currency"] == "USD" and r["original_amount"] == 20 for r in gpt)


def test_events_for_detectors(demo_ledger):
    _, _, events = demo_ledger
    kinds = {(e["kind"], e["merchant_id"]) for e in events}
    assert ("price_change", "netflix") in kinds
    assert ("cancellation", "cultfit") in kinds
    trial = [e for e in events if e["kind"] == "renewal" and e["merchant_id"] == "disney_hotstar"]
    assert trial and trial[0]["is_trial"]
