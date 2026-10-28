"""OCR line rebuilding and its privacy property, with a fake PaddleOCR engine
(no model download). The real engine runs only with LEAK_TEST_OCR=1."""

import os
from pathlib import Path

import pytest

from leak.ingest import ocr
from leak.ingest.ocr import OcrConfig, boxes_to_text, normalize_ocr_digits
from leak.llm.provider import Message
from leak.privacy.guard import EgressGuard
from leak.privacy.redact import PrivacyProfile, Redactor, Vault

STMTS = Path(__file__).parents[1] / "demo" / "sample_data" / "statements"


def test_boxes_become_rows_in_reading_order():
    texts = ["649.00", "05/04/26", "NETFLIX", "header", "doubtful"]
    boxes = [
        [300, 101, 360, 119],  # same row as the date, further right
        [10, 100, 80, 120],
        [100, 102, 200, 118],
        [10, 50, 90, 70],
        [10, 150, 90, 170],
    ]
    scores = [0.99, 0.98, 0.97, 0.95, 0.2]
    assert boxes_to_text(texts, boxes, scores, min_score=0.5) == "header\n05/04/26 NETFLIX 649.00"


@pytest.mark.parametrize(
    "raw,fixed",
    [
        ("Account No : 5O1OO234567890", "Account No : 50100234567890"),
        ("O5/04/26 NETFLIX 1,2l4.OO", "05/04/26 NETFLIX 1,214.00"),
        ("4111 1lll 1111 1111", "4111 1111 1111 1111"),
        ("SBI Card BOSS Ltd", "SBI Card BOSS Ltd"),  # words untouched
    ],
)
def test_normalize_ocr_digits(raw, fixed):
    assert normalize_ocr_digits(raw) == fixed


def test_misread_account_number_is_still_masked(tmp_path):
    """Without normalisation "5O1OO234567890" dodges the digit patterns and
    would be sent to the LLM as-is."""
    red = Redactor(Vault(tmp_path / "v.db"), PrivacyProfile())
    page = boxes_to_text(
        ["Account No :", "5O1OO234567890", "Card", "4111 1lll 1111 1111"],
        [[0, 0, 100, 20], [110, 0, 300, 20], [0, 40, 50, 60], [60, 40, 300, 60]],
        [0.9, 0.9, 0.9, 0.9],
    )
    masked = red.redact(page)
    assert "50100234567890" not in masked and "1111 1111" not in masked
    assert "[ACCT_" in masked and "[CARD_" in masked
    EgressGuard(red).verify("", [Message(role="user", text=masked)])


class FakeEngine:
    """Returns `pages` one at a time, one box per line, like PP-OCR would."""

    def __init__(self, pages: list[str]):
        self.pages = list(pages)
        self.calls = 0

    def predict(self, img):
        self.calls += 1
        lines = self.pages.pop(0).splitlines()
        return [
            {
                "rec_texts": lines,
                "rec_boxes": [[0, 30 * i, 500, 30 * i + 20] for i in range(len(lines))],
                "rec_scores": [0.99] * len(lines),
            }
        ]


@pytest.fixture
def fake_engine(monkeypatch):
    def install(pages):
        engine = FakeEngine(pages)
        monkeypatch.setattr(ocr, "_engine", lambda lang: engine)
        return engine

    return install


def test_disabled_ocr_refuses(tmp_path, fake_engine):
    from PIL import Image

    from leak.ingest.statements import UnsupportedStatement, load_statement_pages

    img = tmp_path / "s.png"
    Image.new("RGB", (50, 50), "white").save(img)
    with pytest.raises(UnsupportedStatement, match="disabled"):
        load_statement_pages(img, ocr=OcrConfig(enabled=False))


@pytest.mark.skipif(
    not os.environ.get("LEAK_TEST_OCR"), reason="set LEAK_TEST_OCR=1 (downloads models)"
)
@pytest.mark.ocr
def test_real_paddleocr_reads_a_rasterised_statement(tmp_path):
    """End to end with the real engine: OCR of a rendered HDFC page finds the
    same transactions as its text layer."""
    from leak.ingest.banks.hdfc import HdfcSavings
    from leak.ingest.ocr import ocr_images, render_pdf_pages
    from leak.ingest.statements import pdf_pages_text, unlock_pdf

    data = unlock_pdf(STMTS / "hdfc_savings_apr-sep_2026.pdf", ["ASHA0101"])
    want = HdfcSavings().parse(pdf_pages_text(data))
    got = HdfcSavings().parse(ocr_images(render_pdf_pages(data)))
    key = lambda ln: (ln.txn_date, ln.amount, ln.direction)  # noqa: E731
    assert sorted(map(key, got.lines)) == sorted(map(key, want.lines))
