"""
Website price-feed importer (YML / Rozetka XML from svyou.ua).

Pulls the public YML catalog and upserts it into the `site_offers` table, keyed to
BAS products via vendor_code = products.code (Артикул). Gives the agent retail price,
stock, rich descriptions, images and product-page URLs for replies and offers (КП).

The feed (~13 MB, ~9k offers) is parsed with iterparse so memory stays flat. The feed
host 403s the default client UA, so we send a browser User-Agent.
"""
import logging
import os
from io import BytesIO
from xml.etree import ElementTree as ET

import asyncpg
import httpx

logger = logging.getLogger(__name__)

SITE_XML_URL = os.getenv("SITE_XML_URL", "https://svyou.ua/price/rozetka.xml")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124 Safari/537.36")


def _text(offer: ET.Element, tag: str, default: str = "") -> str:
    el = offer.find(tag)
    return (el.text or "").strip() if el is not None and el.text else default


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_offers(xml_bytes: bytes) -> list[dict]:
    """Stream the YML and return a list of offer dicts."""
    offers: list[dict] = []
    for _event, el in ET.iterparse(BytesIO(xml_bytes), events=("end",)):
        if el.tag != "offer":
            continue
        offers.append({
            "offer_id": el.get("id", "") or "",
            "available": str(el.get("available", "true")).lower() != "false",
            "vendor_code": _text(el, "vendorCode"),
            "name": _text(el, "name"),
            "url": _text(el, "url"),
            "category_id": _text(el, "categoryId"),
            "price": _num(_text(el, "price")),
            "currency": _text(el, "currencyId", "UAH"),
            "vendor": _text(el, "vendor"),
            "picture": _text(el, "picture"),
            "description": _text(el, "description"),
            "stock": _num(_text(el, "quantity_in_stock")),
        })
        el.clear()  # free the parsed subtree — keeps memory flat on a 13 MB feed
    return offers


async def fetch_feed(url: str = SITE_XML_URL) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def sync_site_offers(pool: asyncpg.Pool, url: str = SITE_XML_URL) -> int:
    """Fetch the feed and upsert every offer. Returns the number of offers upserted."""
    try:
        raw = await fetch_feed(url)
    except Exception as e:
        logger.error(f"[SITE] fetch failed ({url}): {e}")
        return 0

    offers = _parse_offers(raw)
    if not offers:
        logger.info("[SITE] no offers parsed")
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO site_offers (offer_id, vendor_code, name, url, category_id,
                                     price, currency, vendor, picture, description,
                                     available, stock, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now())
            ON CONFLICT (offer_id) DO UPDATE SET
                vendor_code = EXCLUDED.vendor_code,
                name        = EXCLUDED.name,
                url         = EXCLUDED.url,
                category_id = EXCLUDED.category_id,
                price       = EXCLUDED.price,
                currency    = EXCLUDED.currency,
                vendor      = EXCLUDED.vendor,
                picture     = EXCLUDED.picture,
                description = EXCLUDED.description,
                available   = EXCLUDED.available,
                stock       = EXCLUDED.stock,
                updated_at  = now()
            """,
            [
                (o["offer_id"], o["vendor_code"], o["name"], o["url"], o["category_id"],
                 o["price"], o["currency"], o["vendor"], o["picture"], o["description"],
                 o["available"], o["stock"])
                for o in offers if o["offer_id"]
            ],
        )
    logger.info(f"[SITE] upserted {len(offers)} offers from {url}")
    return len(offers)


async def lookup_offer(pool: asyncpg.Pool, vendor_code: str = "", name: str = "") -> dict | None:
    """Find a site offer by vendor_code (exact) or name (fuzzy). For pricing КП lines."""
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            if vendor_code:
                row = await conn.fetchrow(
                    "SELECT * FROM site_offers WHERE vendor_code = $1 LIMIT 1", vendor_code)
                if row:
                    return dict(row)
            if name:
                row = await conn.fetchrow(
                    "SELECT * FROM site_offers "
                    "WHERE to_tsvector('simple', name) @@ plainto_tsquery('simple', $1) "
                    "ORDER BY stock DESC LIMIT 1", name)
                if row:
                    return dict(row)
    except Exception as e:
        logger.error(f"[SITE] lookup_offer error: {e}")
    return None
