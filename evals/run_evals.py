"""Leak evals: extraction precision/recall/F1, ledger dedupe, cost per run.

    uv run python evals/run_evals.py            # offline: replays the committed cassette
    uv run python evals/run_evals.py --live     # calls Gemini for cache misses, records them

Ground truth comes from demo/generate_sample_data.py (it writes
evals/fixtures/demo_labels.jsonl alongside the synthetic data), so labels can't
drift from the data. Only synthetic data is ever sent to the model here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from leak.config import load_settings  # noqa: E402
from leak.extract.extractor import JsonlCache  # noqa: E402
from leak.extract.run import run_extract  # noqa: E402
from leak.extract.schemas import DocExtraction, Record  # noqa: E402
from leak.ingest.models import Document  # noqa: E402
from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan  # noqa: E402
from leak.ledger.merchants import default_catalog, resolve_extracted  # noqa: E402

LABELS = ROOT / "evals" / "fixtures" / "demo_labels.jsonl"
EXPECTED_FINDINGS = ROOT / "evals" / "fixtures" / "demo_findings.json"
LIVE_COSTS = ROOT / "evals" / "live_costs.json"
CASSETTES = ROOT / "evals" / "cassettes"
RESULTS = ROOT / "evals" / "results.json"

Key = tuple[str, str | None, str | None]  # (kind, merchant id, amount)


def _amount(x: Any) -> str | None:
    return None if x is None else f"{Decimal(str(x)):.2f}"


def predicted_keys(doc: Document, ex: DocExtraction) -> Counter[Key]:
    cat = default_catalog()
    out: Counter[Key] = Counter()
    seen_charges: set[tuple[str | None, str | None]] = set()
    for r in ex.records():
        m = resolve_extracted(cat, r.merchant, getattr(r, "plan", None), doc.sender, doc.title)
        mid = m.id if m else None
        kind, amount = _kind_amount(r)
        if kind == "charge":  # txn + receipt for one payment = one charge
            if (mid, amount) in seen_charges:
                continue
            seen_charges.add((mid, amount))
        out[(kind, mid, amount)] += 1
    return out


def _kind_amount(r: Record) -> tuple[str, str | None]:
    if r.kind in ("transaction", "receipt"):
        return "charge", _amount(getattr(r, "amount", None))
    if r.kind == "price_change":
        return "price_change", _amount(getattr(r, "new_amount", None))
    if r.kind == "renewal":
        return "renewal", _amount(getattr(r, "amount", None))
    return r.kind, None


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3),
            "tp": tp, "fp": fp, "fn": fn}  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call Gemini on cassette misses")
    args = ap.parse_args()

    labels = [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]
    truth = {
        x["evidence_id"]: Counter(
            (r["kind"], r["merchant"], _amount(r["amount"])) for r in x["records"]
        )
        for x in labels
    }

    with tempfile.TemporaryDirectory() as tmp:
        s = load_settings(overlay=DEMO_DIR / "demo.toml")
        s.paths.data_dir = Path(tmp)
        run_scan(s, **demo_inputs())
        if not args.live:
            os.environ.pop("GEMINI_API_KEY", None)  # offline: cassette only, never the network
        cassette = JsonlCache(CASSETTES / f"extract-{s.llm.extract_model}.jsonl")
        report = run_extract(s, cache=cassette)
        store, _ = open_store(s)
        from leak.agent.auditor import detect_only

        detected = detect_only(store)
        docs = {d.id: d for d in store.documents()}
        extractions = store.extractions()
        ledger = store.ledger()

    # --- extraction P/R/F1 per kind ----------------------------------------------
    per_kind: dict[str, Counter[str]] = {}
    errors: list[str] = []
    for eid, want in truth.items():
        doc = docs.get(eid)
        got = predicted_keys(doc, extractions[eid]) if doc and eid in extractions else Counter()
        for key in set(want) | set(got):
            c = per_kind.setdefault(key[0], Counter())
            tp = min(want[key], got[key])
            c["tp"] += tp
            c["fp"] += got[key] - tp
            c["fn"] += want[key] - tp
            if got[key] != want[key]:
                errors.append(f"{eid}: {key} expected {want[key]} got {got[key]}")
    total = Counter[str]()
    for c in per_kind.values():
        total.update(c)
    metrics = {k: prf(c["tp"], c["fp"], c["fn"]) for k, c in sorted(per_kind.items())}
    metrics["overall"] = prf(total["tp"], total["fp"], total["fn"])

    # --- ledger dedupe: an SMS/email-only entry that has a matching statement line
    #     means a missed merge (the same charge counted twice).
    stmt = [r for r in ledger if "statement" in r["sources"].split(",")]
    missed = 0
    for r in ledger:
        if "statement" in r["sources"].split(",") or not r["merchant_id"]:
            continue
        for st in stmt:
            same = st["merchant_id"] == r["merchant_id"] and st["direction"] == r["direction"]
            close = abs(st["amount_inr"] - r["amount_inr"]) <= max(1.0, 0.06 * st["amount_inr"])
            days = abs(
                (date.fromisoformat(st["txn_date"]) - date.fromisoformat(r["txn_date"])).days
            )
            if same and close and days <= 3:
                missed += 1
                break
    # --- detection P/R on planted scenarios ------------------------------------------------
    want_f = {tuple(x) for x in json.loads(EXPECTED_FINDINGS.read_text())["expected"]}
    got_f = {(f.type, f.merchant_id) for f in detected}
    det_tp = len(want_f & got_f)
    detection = prf(det_tp, len(got_f - want_f), len(want_f - got_f))
    detection["missed"] = sorted(map(list, want_f - got_f))  # type: ignore[assignment]
    detection["unexpected"] = sorted(map(list, got_f - want_f))  # type: ignore[assignment]

    x = report.extract
    if args.live and x.llm_calls:
        live = json.loads(LIVE_COSTS.read_text())
        live["extraction"] = {
            "model": s.llm.extract_model,
            "documents_via_llm": x.llm_docs,
            "llm_calls": x.llm_calls,
            "input_tokens": x.input_tokens,
            "output_tokens": x.output_tokens,
            "usd": f"{x.cost_usd:.4f}",
            "inr": f"{x.cost_inr:.2f}",
        }
        LIVE_COSTS.write_text(json.dumps(live, indent=2) + "\n")
    out = {
        "model": s.llm.extract_model,
        "extraction": metrics,
        "detection": detection,
        "documents": {"total": x.documents, "by_sms_rules": x.by_rules,
                      "cache_hits": x.cache_hits, "llm_docs": x.llm_docs,
                      "llm_calls": x.llm_calls, "failed": x.failed_docs,
                      "ungrounded_dropped": x.dropped_ungrounded},
        "ledger": {"entries": report.ledger.entries, "multi_source": report.ledger.multi_source,
                   "missed_merges": missed,
                   "unresolved_merchants": report.ledger.unresolved_merchants},
        "live_costs": json.loads(LIVE_COSTS.read_text()),
        "cost_this_run": {"usd": f"{x.cost_usd:.4f}", "inr": f"{x.cost_inr:.2f}",
                          "input_tokens": x.input_tokens, "output_tokens": x.output_tokens},
    }  # fmt: skip
    RESULTS.write_text(json.dumps(out, indent=2) + "\n")

    print(f"\nExtraction ({s.llm.extract_model}) on {len(truth)} labelled docs")
    print(f"{'kind':<14}{'P':>7}{'R':>7}{'F1':>7}{'tp':>5}{'fp':>5}{'fn':>5}")
    for k, m in metrics.items():
        print(f"{k:<14}{m['precision']:>7.3f}{m['recall']:>7.3f}{m['f1']:>7.3f}"
              f"{m['tp']:>5}{m['fp']:>5}{m['fn']:>5}")  # fmt: skip
    dm = out["detection"]
    print(
        f"\nDetection (deterministic detectors) on {len(want_f)} planted findings: "
        f"P {dm['precision']:.3f} · R {dm['recall']:.3f} · F1 {dm['f1']:.3f}"
    )
    if dm["missed"] or dm["unexpected"]:
        print(f"  missed: {dm['missed']}  unexpected: {dm['unexpected']}")
    d = out["documents"]
    print(f"\ndocs: {d['by_sms_rules']} by SMS rules · {d['cache_hits']} from cache · "
          f"{d['llm_docs']} via LLM ({d['llm_calls']} calls) · {d['failed']} failed")  # fmt: skip
    lg = out["ledger"]
    print(
        f"ledger: {lg['entries']} entries · {lg['multi_source']} multi-source · "
        f"{lg['missed_merges']} missed merges · {lg['unresolved_merchants']} unresolved"
    )
    c = out["cost_this_run"]
    print(f"cost this run: ${c['usd']} (₹{c['inr']}) · {c['input_tokens']} in / "
          f"{c['output_tokens']} out tokens")  # fmt: skip
    if errors:
        print("\nmismatches:")
        for e in errors:
            print("  " + e)
    if x.failed_docs and not args.live:
        print("\ncassette misses: run with --live to record them", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
