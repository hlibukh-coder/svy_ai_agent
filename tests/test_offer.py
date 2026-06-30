"""Tests for src/offer.py — КП PDF rendering (pure, no network/DB)."""
import os

os.environ.setdefault("USE_MOCK", "true")

from src.offer import render_offer_pdf, _fmt


def test_fmt_groups_and_trims():
    assert _fmt(1234.5) == "1 234.50"
    assert _fmt(40600) == "40 600"          # trailing .00 trimmed
    assert _fmt(2) == "2"


def test_render_offer_pdf_is_valid_pdf_with_cyrillic():
    lines = [
        {"name": "Заклепка витяжна 4х10 нерж А2", "article": "MF-4010",
         "qty": 5000, "price": 1.25, "sum": 6250.0},
        {"name": "Пилосос промисловий", "article": "SA-125",
         "qty": 1, "price": 40600, "sum": 40600.0},
    ]
    data = render_offer_pdf("ПРЕМ'ЄР ФУД", "+380504442888", "ТОВ ПРЕМ'ЄР",
                            lines, "Ціни дійсні 14 днів", "КП-TEST1234")
    assert isinstance(data, (bytes, bytearray))
    assert data[:5] == b"%PDF-"
    assert len(data) > 2000
