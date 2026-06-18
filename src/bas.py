import os
import logging
import aiohttp
from urllib.parse import quote
import yarl
from src.prompt import MOCK_CLIENTS, MOCK_PRODUCTS

logger = logging.getLogger(__name__)

USE_MOCK = os.getenv("USE_MOCK", "true").lower() == "true"
BAS_URL = os.getenv("BAS_URL", "")
BAS_USER = os.getenv("BAS_USER", os.getenv("BAS_LOGIN", ""))
BAS_PASS = os.getenv("BAS_PASS", os.getenv("BAS_PASSWORD", ""))


def _auth():
    return aiohttp.BasicAuth(BAS_USER, BAS_PASS)


def _url(raw: str) -> yarl.URL:
    """Convert a raw URL (possibly with Cyrillic chars) to a yarl.URL safe for aiohttp."""
    encoded = quote(raw, safe=":/?&=$@,!'()*+;[]%-._~")
    return yarl.URL(encoded, encoded=True)


def _get_pool():
    """Return asyncpg pool if sync module is available and initialised."""
    try:
        from sync import scheduler_sync
        return scheduler_sync.get_pool()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------

async def get_client(phone: str) -> dict | None:
    if USE_MOCK:
        normalized = phone if phone.startswith("+") else f"+{phone}"
        client = MOCK_CLIENTS.get(normalized)
        if client:
            result = {k: v for k, v in client.items() if k != "orders"}
            result["phone"] = normalized
            logger.info(f"[MOCK] get_client({phone}) -> {result['id']}")
            return result
        logger.info(f"[MOCK] get_client({phone}) -> None")
        return None

    # Try PG first
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ref_key, name, code, phone, company, city FROM clients "
                    "WHERE phone LIKE $1 AND deleted = false LIMIT 1",
                    f"%{phone.lstrip('+')}%",
                )
            if row:
                return {
                    "id": row["ref_key"],
                    "name": row["name"],
                    "company": row["company"] or "",
                    "city": row["city"] or "",
                    "phone": phone,
                }
        except Exception as e:
            logger.error(f"[PG] get_client error: {e}")

    # Fallback: direct OData
    url = f"{BAS_URL}/Catalog_Контрагенты?$filter=contains(НомерТелефона,'{phone}')&$top=1&$format=json"
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                items = data.get("value", [])
                if not items:
                    return None
                item = items[0]
                return {
                    "id": item.get("Ref_Key"),
                    "name": item.get("Description", ""),
                    "company": item.get("НаименованиеПолное", ""),
                    "city": item.get("Город", ""),
                    "phone": phone,
                }
    except Exception as e:
        logger.error(f"get_client error: {e}")
        return None


# ---------------------------------------------------------------------------
# get_orders
# ---------------------------------------------------------------------------

async def get_orders(client_id: str) -> list:
    if USE_MOCK:
        for client in MOCK_CLIENTS.values():
            if client["id"] == client_id:
                logger.info(f"[MOCK] get_orders({client_id}) -> {len(client['orders'])} orders")
                return client["orders"]
        return []

    # Try PG first
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ref_key, number, date, amount FROM orders "
                    "WHERE client_ref_key = $1 ORDER BY date DESC LIMIT 10",
                    client_id,
                )
            return [
                {
                    "id": r["ref_key"],
                    "number": r["number"],
                    "date": str(r["date"])[:10] if r["date"] else "",
                    "total": float(r["amount"] or 0),
                    "items": [],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[PG] get_orders error: {e}")

    # Fallback: direct OData
    url = (
        f"{BAS_URL}/Document_ЗаказПокупателя"
        f"?$filter=Контрагент_Key eq guid'{client_id}'&$orderby=Date desc&$top=10&$format=json"
    )
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                items = data.get("value", [])
                orders = []
                for item in items:
                    orders.append({
                        "id": item.get("Ref_Key"),
                        "date": item.get("Date", "")[:10],
                        "total": item.get("СуммаДокумента", 0),
                        "items": [],
                    })
                return orders
    except Exception as e:
        logger.error(f"get_orders error: {e}")
        return []


# ---------------------------------------------------------------------------
# get_products
# ---------------------------------------------------------------------------

async def get_products(query: str) -> list:
    if USE_MOCK:
        q = query.lower()
        results = [
            p for p in MOCK_PRODUCTS
            if q in p["name"].lower() or q in p["article"].lower()
        ]
        logger.info(f"[MOCK] get_products('{query}') -> {len(results)} results")
        return results

    # Try PG first
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ref_key, name, code, price, stock
                    FROM products
                    WHERE deleted = false
                      AND (
                        code ILIKE $1
                        OR name ILIKE $1
                        OR to_tsvector('simple', name) @@ plainto_tsquery('simple', $2)
                      )
                    LIMIT 5
                    """,
                    f"%{query}%",
                    query,
                )
            if rows:
                return [
                    {
                        "article": r["code"],
                        "name": r["name"],
                        "price": float(r["price"] or 0),
                        "stock": float(r["stock"] or 0),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"[PG] get_products error: {e}")

    # Fallback: direct OData
    url_article = f"{BAS_URL}/Catalog_Номенклатура?$filter=Код eq '{query}'&$format=json"
    url_name = f"{BAS_URL}/Catalog_Номенклатура?$filter=contains(Description,'{query}')&$top=5&$format=json"

    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url_article), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                items = data.get("value", [])
                if not items:
                    async with session.get(_url(url_name), timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                        data2 = await resp2.json(content_type=None)
                        items = data2.get("value", [])
                return [
                    {
                        "article": i.get("Код", ""),
                        "name": i.get("Description", ""),
                        "price": i.get("ЦенаПродажи", 0),
                        "stock": i.get("Остаток", 0),
                    }
                    for i in items
                ]
    except Exception as e:
        logger.error(f"get_products error: {e}")
        return []


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

async def get_order_status(order_id: str) -> dict | None:
    if USE_MOCK:
        mock_statuses = {
            "order_001": {"id": "order_001", "status": "shipped", "status_label": "Відправлено", "date": "15.05.2026", "delivery_date": "18.05.2026"},
            "order_002": {"id": "order_002", "status": "processing", "status_label": "В обробці", "date": "01.06.2026", "delivery_date": None},
        }
        result = mock_statuses.get(order_id)
        logger.info(f"[MOCK] get_order_status({order_id}) -> {result}")
        return result

    url = f"{BAS_URL}/Document_ЗаказПокупателя(guid'{order_id}')?$format=json"
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                if not data:
                    return None
                return {
                    "id": data.get("Ref_Key", order_id),
                    "status": data.get("Статус", ""),
                    "status_label": data.get("СтатусНаименование", ""),
                    "date": data.get("Date", "")[:10],
                    "delivery_date": data.get("ДатаДоставки", None),
                }
    except Exception as e:
        logger.error(f"get_order_status error: {e}")
        return None


# ---------------------------------------------------------------------------
# check_supplier
# ---------------------------------------------------------------------------

async def check_supplier(product_name: str, qty: int = 1) -> dict:
    """Check product availability at supplier (mock always returns estimated data)."""
    if USE_MOCK:
        logger.info(f"[MOCK] check_supplier('{product_name}', qty={qty})")
        return {
            "available": True,
            "qty": qty * 2,
            "lead_days": 3,
            "note": "Є на складі постачальника, термін поставки 3 дні",
        }

    logger.warning(f"check_supplier: no real supplier API configured for '{product_name}'")
    return {
        "available": False,
        "qty": 0,
        "lead_days": None,
        "note": "Інформація від постачальника недоступна",
    }


# ---------------------------------------------------------------------------
# get_inactive_clients
# ---------------------------------------------------------------------------

async def get_inactive_clients(days_threshold: int = 60) -> list:
    """Return clients who haven't ordered for `days_threshold` days."""
    if USE_MOCK:
        from datetime import datetime, timedelta
        cutoff = datetime.now().date() - timedelta(days=days_threshold)
        result = []
        for phone, client in MOCK_CLIENTS.items():
            orders = client.get("orders", [])
            if not orders:
                continue
            try:
                last_date = datetime.strptime(orders[0]["date"], "%d.%m.%Y").date()
            except Exception:
                continue
            if last_date < cutoff:
                result.append({
                    "phone": phone,
                    "name": client.get("name", ""),
                    "last_order_date": orders[0]["date"],
                    "last_items": orders[0].get("items", []),
                })
        logger.info(f"[MOCK] get_inactive_clients(days={days_threshold}) -> {len(result)} clients")
        return result

    # Try PG first
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT c.ref_key, c.name, c.phone,
                           MAX(o.date) AS last_order_date
                    FROM clients c
                    LEFT JOIN orders o ON o.client_ref_key = c.ref_key
                    WHERE c.deleted = false
                    GROUP BY c.ref_key, c.name, c.phone
                    HAVING MAX(o.date) < CURRENT_DATE - $1::int
                        OR MAX(o.date) IS NULL
                    ORDER BY last_order_date ASC NULLS FIRST
                    """,
                    days_threshold,
                )
            return [
                {
                    "client_id": r["ref_key"],
                    "name": r["name"],
                    "phone": r["phone"] or "",
                    "last_order_date": str(r["last_order_date"])[:10] if r["last_order_date"] else "",
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[PG] get_inactive_clients error: {e}")

    # Fallback: direct OData
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days_threshold)).strftime("%Y-%m-%dT00:00:00")
    url = (
        f"{BAS_URL}/Document_ЗаказПокупателя"
        f"?$filter=Date lt datetime'{cutoff}'&$orderby=Контрагент_Key,Date desc&$format=json"
    )
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                items = data.get("value", [])
                seen = set()
                result = []
                for item in items:
                    cid = item.get("Контрагент_Key")
                    if cid in seen:
                        continue
                    seen.add(cid)
                    result.append({
                        "client_id": cid,
                        "last_order_date": item.get("Date", "")[:10],
                    })
                return result
    except Exception as e:
        logger.error(f"get_inactive_clients error: {e}")
        return []


# ---------------------------------------------------------------------------
# get_new_products
# ---------------------------------------------------------------------------

async def get_new_products(since_days: int = 7) -> list:
    """Return products added/marked as new in the last `since_days` days."""
    if USE_MOCK:
        return [p for p in MOCK_PRODUCTS if p.get("new", False)]

    # Try PG first
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ref_key, name, code, price, stock
                    FROM products
                    WHERE deleted = false
                      AND updated_at >= now() - ($1 || ' days')::interval
                    ORDER BY updated_at DESC
                    """,
                    str(since_days),
                )
            if rows:
                return [
                    {
                        "article": r["code"],
                        "name": r["name"],
                        "price": float(r["price"] or 0),
                        "stock": float(r["stock"] or 0),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"[PG] get_new_products error: {e}")

    # Fallback: direct OData
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%dT00:00:00")
    url = (
        f"{BAS_URL}/Catalog_Номенклатура"
        f"?$filter=ДатаДобавления gt datetime'{cutoff}'&$format=json"
    )
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                return [
                    {
                        "article": i.get("Код", ""),
                        "name": i.get("Description", ""),
                        "price": i.get("ЦенаПродажи", 0),
                        "stock": i.get("Остаток", 0),
                    }
                    for i in data.get("value", [])
                ]
    except Exception as e:
        logger.error(f"get_new_products error: {e}")
        return []


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------

async def create_order(
    client_id: str,
    client_name: str,
    client_phone: str,
    company: str,
    city: str,
    items: list,
    comment: str = "",
) -> dict:
    if USE_MOCK:
        order_id = f"order_mock_{client_id[:8]}"
        total = sum(i.get("price", 0) * i.get("qty", 0) for i in items)
        logger.info(f"[MOCK] create_order for {client_name} -> {order_id}, total={total}")
        return {"success": True, "order_id": order_id, "total": total}

    payload = {
        "Контрагент_Key": client_id,
        "Комментарий": comment,
        "Товары": [
            {
                "Номенклатура_Key": i.get("article"),
                "Количество": i.get("qty"),
                "Цена": i.get("price"),
                "Сумма": i.get("price", 0) * i.get("qty", 0),
            }
            for i in items
        ],
    }
    url = f"{BAS_URL}/Document_ЗаказПокупателя"
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                return {"success": True, "order_id": data.get("Ref_Key", ""), "total": 0}
    except Exception as e:
        logger.error(f"create_order error: {e}")
        return {"success": False, "error": str(e)}
