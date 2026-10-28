"""Local OCR for scanned statements and photos of statements (PaddleOCR).

    page image ──PP-OCR (on this machine)──> text boxes ──rows by y, x-order──> lines

Everything runs locally: pages are rendered in memory, nothing is uploaded.
The only network traffic is PaddleOCR fetching its model weights on first use
(no user data); pre-seed its cache to run fully offline.

Output matches what `pdfplumber` gives the bank parsers: one string per page,
one line per visual row, words joined by single spaces.
"""

from __future__ import annotations

import io
import re
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})


class OcrConfig(BaseModel):
    enabled: bool = True
    lang: str = "en"
    min_score: float = 0.5  # drop boxes the recogniser itself doubts
    dpi: int = 300


class OcrUnavailable(RuntimeError):
    pass


@cache
def _engine(lang: str) -> Any:
    """Build once per process: model load takes seconds."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as e:  # pragma: no cover - core dependency
        raise OcrUnavailable(f"paddleocr is not installed: {e}") from e
    try:
        return PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except Exception as e:  # first run downloads weights; offline / bad lang end here
        raise OcrUnavailable(f"could not load PaddleOCR models ({type(e).__name__}: {e})") from e


# --- input -> page images --------------------------------------------------------


def render_pdf_pages(
    data: bytes, dpi: int = 300, pages: list[int] | None = None
) -> list[np.ndarray]:
    """Rasterise (0-based) `pages` of a decrypted PDF to RGB arrays, in memory."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(io.BytesIO(data))
    try:
        idx = range(len(pdf)) if pages is None else pages
        return [np.asarray(pdf[i].render(scale=dpi / 72).to_pil().convert("RGB")) for i in idx]
    finally:
        pdf.close()


def load_image(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        return np.asarray(ImageOps.exif_transpose(im).convert("RGB"))


# --- boxes -> lines --------------------------------------------------------------


def boxes_to_text(texts: list[str], boxes: Any, scores: list[float], min_score: float = 0.5) -> str:
    """Rebuild reading-order lines from OCR boxes ([x1, y1, x2, y2] each).

    A box joins the current row when its vertical centre lies within half a
    line-height of the row's centre; rows are then read left to right.
    """
    items = [
        (float(b[0]), float(b[1]), float(b[2]), float(b[3]), t.strip())
        for t, b, s in zip(texts, boxes, scores, strict=True)
        if s >= min_score and t.strip()
    ]
    items.sort(key=lambda it: (it[1] + it[3]) / 2)
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for it in items:
        cy, h = (it[1] + it[3]) / 2, it[3] - it[1]
        if rows:
            row = rows[-1]
            row_cy = sum((r[1] + r[3]) / 2 for r in row) / len(row)
            row_h = sum(r[3] - r[1] for r in row) / len(row)
            if abs(cy - row_cy) <= max(row_h, h) / 2:
                row.append(it)
                continue
        rows.append([it])
    lines = [" ".join(r[4] for r in sorted(row, key=lambda r: r[0])) for row in rows]
    return normalize_ocr_digits("\n".join(lines))


# OCR confuses these with digits. Inside a number they are almost always digits,
# and fixing them matters for privacy, not just accuracy: an account number read
# as "5O1002" slips past the redactor's digit patterns and would reach the LLM.
_CONFUSABLE = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1",
                             "S": "5", "B": "8"})  # fmt: skip
_NUMBERISH = re.compile(r"(?<!\S)[\dOoDlI|SB,./:-]+(?!\S)")


def normalize_ocr_digits(text: str) -> str:
    """Map look-alike letters to digits in tokens made only of digits and
    look-alikes ("5O1OO2", "1lll"). Needs a real digit and 4+ characters, so
    words ("SBI", "BOSS") are left alone; when unsure, it errs toward digits,
    which the redactor then masks."""

    def fix(m: re.Match[str]) -> str:
        tok = m.group(0)
        return (
            tok.translate(_CONFUSABLE) if len(tok) >= 4 and any(c.isdigit() for c in tok) else tok
        )

    return _NUMBERISH.sub(fix, text)


def ocr_images(images: list[np.ndarray], cfg: OcrConfig | None = None) -> list[str]:
    cfg = cfg or OcrConfig()
    if not cfg.enabled:
        raise OcrUnavailable("OCR is disabled ([statements.ocr] enabled = false)")
    engine = _engine(cfg.lang)
    pages: list[str] = []
    for img in images:
        text_parts = []
        for res in engine.predict(img):
            text_parts.append(
                boxes_to_text(
                    list(res["rec_texts"]),
                    res["rec_boxes"],
                    [float(s) for s in res["rec_scores"]],
                    cfg.min_score,
                )
            )
        pages.append("\n".join(p for p in text_parts if p))
    return pages
