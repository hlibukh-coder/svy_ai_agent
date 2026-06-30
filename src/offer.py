"""
Commercial offer (КП) builder.

Turns an operator instruction ("выстави КП на позицию X, N штук, по цене Y") into a
professional PDF a manager can send to the client. Each line is priced from the
operator's explicit price, else the website feed (`site_offers`), else BAS.

Cyrillic is rendered via the bundled assets/fonts/DejaVuSans.ttf (fpdf2 core fonts are
latin-1 only). Returns an in-memory doc dict compatible with channel adapter send_file.
"""
import logging
import os
import uuid
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = ROOT / "assets" / "fonts" / "DejaVuSans.ttf"

SELLER_NAME = os.getenv("OFFER_SELLER_NAME", "SVYOU.UA")
SELLER_PHONE = os.getenv("OFFER_SELLER_PHONE", "0800445432")


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def resolve_lines(items: list[dict]) -> list[dict]:
    """Normalize each requested item into {name, article, qty, price, sum}.

    Price precedence: explicit price from the operator → site_offers (retail) → BAS.
    Name/article are filled from the catalog when the operator gave only one of them.
    """
    from sync import scheduler_sync
    from sync import site_catalog
    pool = scheduler_sync.get_pool()

    lines: list[dict] = []
    for it in items:
        name = (it.get("name") or "").strip()
        article = (it.get("article") or it.get("vendor_code") or "").strip()
        qty = _num(it.get("qty") or 1) or 1
        price = it.get("price")

        offer = None
        if pool:
            offer = await site_catalog.lookup_offer(pool, vendor_code=article, name=name)
        if offer:
            name = name or offer.get("name", "")
            article = article or offer.get("vendor_code", "")
            if price in (None, "", 0):
                price = offer.get("price")
        # BAS fallback for name when still missing
        if not name and article:
            try:
                from src import bas
                found = await bas.get_products(article)
                if found:
                    name = found[0].get("name", "") or name
                    if price in (None, "", 0):
                        price = found[0].get("price")
            except Exception:
                pass

        price = _num(price)
        lines.append({
            "name": name or article or "Позиція",
            "article": article,
            "qty": qty,
            "price": price,
            "sum": round(price * qty, 2),
        })
    return lines


def _fmt(n: float) -> str:
    """1234.5 -> '1 234.50' (thin grouping, 2 decimals), trimming trailing .00."""
    s = f"{n:,.2f}".replace(",", " ")
    return s[:-3] if s.endswith(".00") else s


def render_offer_pdf(client_name: str, client_phone: str, company: str,
                     lines: list[dict], comment: str = "", offer_no: str = "") -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    from fpdf.enums import XPos, YPos

    total = round(sum(l["sum"] for l in lines), 2)
    offer_no = offer_no or f"КП-{uuid.uuid4().hex[:8].upper()}"

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("DejaVu", "", str(FONT_PATH))
    pdf.set_font("DejaVu", size=16)
    pdf.cell(0, 10, "КОМЕРЦІЙНА ПРОПОЗИЦІЯ", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("DejaVu", size=10)
    pdf.cell(0, 6, f"№ {offer_no}    від {date.today().isoformat()}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Постачальник: {SELLER_NAME}    тел.: {SELLER_PHONE}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.cell(0, 6, f"Клієнт: {client_name or '—'}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if company:
        pdf.cell(0, 6, f"Компанія: {company}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Телефон: {client_phone or '—'}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # Items table — fpdf2 handles wrapping/column widths.
    pdf.set_font("DejaVu", size=9)
    headings = ("№", "Найменування", "Артикул", "К-сть", "Ціна, грн", "Сума, грн")
    # Headings non-bold (only the regular DejaVu weight is bundled), light grey fill.
    head_style = FontFace(emphasis="", fill_color=(232, 232, 232))
    with pdf.table(col_widths=(8, 78, 28, 16, 26, 28),
                   text_align=("CENTER", "LEFT", "LEFT", "CENTER", "RIGHT", "RIGHT"),
                   first_row_as_headings=True, headings_style=head_style) as table:
        row = table.row()
        for h in headings:
            row.cell(h)
        for i, l in enumerate(lines, 1):
            row = table.row()
            row.cell(str(i))
            row.cell(l["name"])
            row.cell(l["article"] or "—")
            row.cell(_fmt(l["qty"]))
            row.cell(_fmt(l["price"]))
            row.cell(_fmt(l["sum"]))

    pdf.ln(2)
    pdf.set_font("DejaVu", size=12)
    pdf.cell(0, 8, f"Разом: {_fmt(total)} грн", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if comment:
        pdf.set_font("DejaVu", size=9)
        pdf.ln(2)
        pdf.multi_cell(0, 5, f"Коментар: {comment}")

    pdf.set_font("DejaVu", size=8)
    pdf.ln(3)
    pdf.multi_cell(0, 4, "Пропозиція має інформаційний характер. Ціни та наявність "
                         "підтверджує менеджер. Дякуємо за інтерес до нашої продукції!")

    out = pdf.output()
    return bytes(out)


async def build_offer_doc(client_name: str, client_phone: str, company: str,
                          items: list[dict], comment: str = "") -> dict:
    """Resolve prices, render the PDF, and return a send_file-compatible doc dict."""
    lines = await resolve_lines(items)
    offer_no = f"КП-{uuid.uuid4().hex[:8].upper()}"
    pdf_bytes = render_offer_pdf(client_name, client_phone, company, lines, comment, offer_no)
    total = round(sum(l["sum"] for l in lines), 2)
    return {
        "src": pdf_bytes,
        "filename": f"{offer_no}.pdf",
        "mimetype": "application/pdf",
        "offer_no": offer_no,
        "total": total,
        "lines": lines,
    }
