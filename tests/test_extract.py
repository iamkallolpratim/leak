import json
from decimal import Decimal

import pytest

from leak.extract.extractor import (
    Extractor,
    JsonlCache,
    SqliteCache,
    amount_in_text,
    cache_key,
    ground,
)
from leak.extract.schemas import BatchExtraction, DocExtraction, Receipt
from leak.extract.sms_rules import extract_sms
from leak.harness import Budget, ModelPrice
from leak.ingest.models import Document
from leak.llm.fake import ScriptedProvider
from leak.llm.provider import LLMError, LLMResponse, Usage
from leak.privacy.redact import PrivacyProfile, Redactor, Vault
from leak.store import Store

MODEL = "flash"


def doc(i: int, text: str, source: str = "gmail") -> Document:
    return Document(
        id=f"{source}:{i}",
        source=source,
        title=f"t{i}",
        raw_text=text,
        redacted_text=text,
        content_hash=str(i),
    )


def batch_reply(*docs: DocExtraction, inp=1000, out=200) -> LLMResponse:
    return LLMResponse(
        model=MODEL,
        text=BatchExtraction(documents=list(docs)).model_dump_json(),
        usage=Usage(input_tokens=inp, output_tokens=out),
    )


def receipt(eid: str, merchant: str, amount: str) -> DocExtraction:
    return DocExtraction(
        evidence_id=eid,
        is_financial=True,
        receipts=[Receipt(merchant=merchant, amount=Decimal(amount))],
    )


@pytest.fixture
def parts(tmp_path):
    red = Redactor(Vault(tmp_path / "v.db"), PrivacyProfile())
    budget = Budget(
        max_steps=99,
        max_usd=Decimal("1"),
        prices={MODEL: ModelPrice(input=Decimal(1), output=Decimal(2))},
    )
    return red, budget, Store(tmp_path / "s.db")


@pytest.mark.parametrize(
    "text,merchant,amount,acct",
    [
        (
            "Rs.649.00 debited from a/c [ACCT_3] on 05-04-26 to VPA netflix@hdfcbank (UPI Ref No "
            "[NUM_1]).",
            "netflix@hdfcbank",
            "649.00",
            "[ACCT_3]",
        ),
        (
            "Rs.119.00 spent on your SBI Credit Card ending [CARD_3] at SPOTIFY INDIA on 12/04/26. "
            "Trxn",
            "SPOTIFY INDIA",
            "119.00",
            "[CARD_3]",
        ),
        (
            "INR 299.00 spent on ICICI Bank Card [CARD_1] on 30-Jun-26 on HOTSTAR. Avl Limit",
            "HOTSTAR",
            "299.00",
            "[CARD_1]",
        ),
        (
            "ACH Debit of INR 1499.00 from A/c no. [ACCT_2] towards CULTFIT HEALTHCARE "
            "on 12-07-2026.",
            "CULTFIT HEALTHCARE",
            "1499.00",
            "[ACCT_2]",
        ),
    ],
)
def test_sms_rules(text, merchant, amount, acct):
    ex = extract_sms("sms:x", text)
    t = ex.transactions[0]
    assert (t.merchant, t.amount, t.account) == (merchant, Decimal(amount), acct)
    assert t.txn_date is not None


def test_sms_rules_skip_mandates_and_defer_unknown_formats():
    assert (
        extract_sms(
            "s", "UPI-Mandate for NETFLIX of Rs 649.00 created on a/c [ACCT_1]"
        ).transactions
        == []
    )
    assert extract_sms("s", "Your a/c was charged INR 50 for SMS alerts") is None  # -> LLM


def test_amount_grounding():
    assert amount_in_text(Decimal("1499"), "Annual membership: Rs 1,499.00")
    assert amount_in_text(Decimal("20.00"), "Amount paid: $20.00 USD")
    assert not amount_in_text(Decimal("49"), "Rs 1499.00")  # no partial-number matches
    ex, dropped = ground(receipt("d", "X", "999"), "Total Rs 486")
    assert dropped == 1 and ex.receipts == []


def test_batches_cache_and_rerun_costs_nothing(parts):
    red, budget, store = parts
    docs = [doc(i, f"Receipt: Netflix Rs {600 + i}.00") for i in range(5)]
    provider = ScriptedProvider(
        lambda msgs: batch_reply(
            *[
                receipt(f"gmail:{i}", "Netflix", f"{600 + i}")
                for i in range(5)
                if f'id="gmail:{i}"' in msgs[0].text
            ]
        )
    )
    cache = SqliteCache(store.db)
    ex = Extractor(provider, MODEL, red, cache, budget, batch_docs=2)
    results, stats = ex.run(docs)
    assert stats.llm_calls == 3 and stats.llm_docs == 5  # batches of 2,2,1
    assert results["gmail:3"].receipts[0].amount == Decimal("603")
    assert stats.cost_usd == Decimal("0.0042")  # 3 calls x (1000*1 + 200*2)/1M

    budget2 = Budget(max_steps=99, max_usd=Decimal("1"), prices=budget.prices)
    _, again = Extractor(provider, MODEL, red, cache, budget2).run(docs)
    assert (again.cache_hits, again.llm_calls, again.cost_usd) == (5, 0, Decimal(0))


def test_failing_batch_is_split_so_one_bad_doc_only_loses_itself(parts):
    red, budget, store = parts
    docs = [doc(i, f"Rs {100 + i}.00") for i in range(4)]

    def script(msgs):
        if 'id="gmail:2"' in msgs[0].text:
            raise LLMError("model choked")
        ids = [i for i in range(4) if f'id="gmail:{i}"' in msgs[0].text]
        return batch_reply(*[receipt(f"gmail:{i}", "X", f"{100 + i}") for i in ids])

    results, stats = Extractor(
        ScriptedProvider(script), MODEL, red, SqliteCache(store.db), budget, batch_docs=4
    ).run(docs)
    assert stats.failed_docs == 1 and set(results) == {"gmail:0", "gmail:1", "gmail:3"}


def test_budget_stop_keeps_finished_work(parts, tmp_path):
    red, _, _store = parts
    tiny = Budget(
        max_steps=99,
        max_usd=Decimal("0.001"),
        prices={MODEL: ModelPrice(input=Decimal(1), output=Decimal(2))},
    )
    docs = [doc(i, f"Rs {100 + i}.00") for i in range(4)]
    provider = ScriptedProvider(
        lambda msgs: batch_reply(
            *[
                receipt(f"gmail:{i}", "X", f"{100 + i}")
                for i in range(4)
                if f'id="gmail:{i}"' in msgs[0].text
            ]
        )
    )
    cache = JsonlCache(tmp_path / "c.jsonl")
    _results, stats = Extractor(provider, MODEL, red, cache, tiny, batch_docs=1).run(docs)
    assert stats.stopped and "cost budget" in stats.stopped
    assert stats.failed_docs == 3  # first batch paid for + cached, rest skipped
    assert cache.get(cache_key(MODEL, "Rs 100.00")) is not None


def test_no_provider_reports_instead_of_guessing(parts):
    red, budget, store = parts
    results, stats = Extractor(None, MODEL, red, SqliteCache(store.db), budget).run(
        [doc(1, "Rs 10.00")]
    )
    assert results == {} and stats.failed_docs == 1


def test_llm_payload_is_guarded(parts):
    red, budget, store = parts
    provider = ScriptedProvider(
        lambda msgs: batch_reply(DocExtraction(evidence_id="gmail:1", is_financial=False))
    )
    raw = doc(1, "PAN ABCPT1234F card 4111 1111 1111 1111")  # redacted_text left raw on purpose
    Extractor(provider, MODEL, red, SqliteCache(store.db), budget).run([raw])
    sent = json.dumps([[m.model_dump(mode="json") for m in r] for r in provider.requests])
    assert "ABCPT1234F" not in sent and "4111 1111" not in sent


def test_jsonl_cassette_roundtrip(tmp_path):
    c = JsonlCache(tmp_path / "x.jsonl")
    c.put("k", receipt("gmail:1", "Netflix", "649"))
    again = JsonlCache(tmp_path / "x.jsonl").get("k")
    assert again.receipts[0].amount == Decimal("649")
