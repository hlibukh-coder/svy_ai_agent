import json as _json
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
    # BAS login is Cyrillic ("ЮдінСВ"); aiohttp's default latin-1 BasicAuth
    # encoding raises UnicodeEncodeError, so force UTF-8.
    return aiohttp.BasicAuth(BAS_USER, BAS_PASS, "utf-8")


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

    # Try PG first — search by multiple phone variants to handle format differences.
    # BAS stores phones in various formats: "0504442888", "380504442888", "+380504442888".
    # We build a list of digit-only variants and try each.
    pool = _get_pool()
    if pool:
        try:
            import re as _re
            digits = _re.sub(r"[^\d]", "", phone)
            # variants: full (380...), local (0...), last 9 digits (504...)
            variants: list[str] = [digits]
            if digits.startswith("38") and len(digits) >= 11:
                variants.append(digits[2:])   # strip country code → "0504442888"
            if len(digits) >= 9:
                variants.append(digits[-9:])  # last 9 digits → "504442888"

            async with pool.acquire() as conn:
                row = None
                for variant in variants:
                    row = await conn.fetchrow(
                        "SELECT ref_key, name, code, phone, company, city FROM clients "
                        "WHERE phone LIKE $1 AND deleted = false LIMIT 1",
                        f"%{variant}%",
                    )
                    if row:
                        break
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

    # Fallback: direct OData — НомерТелефонаДляПоиска is the correct field (not НомерТелефона)
    url = f"{BAS_URL}/Catalog_Контрагенты?$filter=contains(НомерТелефонаДляПоиска,'{phone}')&$top=1&$format=json"
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
                    "city": "",
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
                    """
                    SELECT o.ref_key, o.number, o.date, o.amount,
                           coalesce(
                               json_agg(
                                   json_build_object('name', p.name, 'qty', oi.qty)
                                   ORDER BY oi.id
                               ) FILTER (WHERE oi.id IS NOT NULL),
                               '[]'::json
                           ) AS items
                    FROM orders o
                    LEFT JOIN order_items oi ON oi.order_ref_key = o.ref_key
                    LEFT JOIN products p ON p.ref_key = oi.product_ref_key
                    WHERE o.client_ref_key = $1
                    GROUP BY o.ref_key, o.number, o.date, o.amount
                    ORDER BY o.date DESC
                    LIMIT 10
                    """,
                    client_id,
                )
            def _parse_items(raw) -> list:
                """asyncpg may return json_agg elements as dicts or JSON strings."""
                if not raw:
                    return []
                result = []
                for item in raw:
                    if isinstance(item, dict):
                        result.append(item)
                    elif isinstance(item, str):
                        try:
                            result.append(_json.loads(item))
                        except Exception:
                            pass
                return result

            return [
                {
                    "id": r["ref_key"],
                    "number": r["number"],
                    "date": str(r["date"])[:10] if r["date"] else "",
                    "total": float(r["amount"] or 0),
                    "items": _parse_items(r["items"]),
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

# Cyrillic→Latin lookalike folding (lowercase). BAS names mix scripts freely:
# "Гайка Din 6923 M8" (Latin M) vs "М8" (Cyrillic М). Fold both query and name to
# one canonical form so size/standard tokens match regardless of script. The SAME
# pair of strings is used in SQL translate() — keep them in sync.
_FOLD_CYR = "авекмнорстухі"
_FOLD_LAT = "abekmhopctyxi"
_FOLD = str.maketrans(_FOLD_CYR, _FOLD_LAT)
_NAME_FOLD_SQL = f"translate(lower(name), '{_FOLD_CYR}', '{_FOLD_LAT}')"
_STOPWORDS = {"з", "із", "зі", "для", "та", "і", "й", "в", "на", "по", "до", "от", "the", "din"}


def _fold(s: str) -> str:
    return s.lower().translate(_FOLD)


def _tokenize_query(query: str) -> tuple[list[str], list[str]]:
    """Split a product query into (required, optional) folded tokens.

    Required = tokens containing a digit (sizes, DIN numbers, dimensions) — the
    selective parts. Dimension chains ("M8х50", "6,4х12,5") are split on the
    separator so format differences in BAS names don't cause misses. Optional =
    descriptive words, used only for ranking. If nothing has a digit, the longest
    word becomes the sole required token so we still narrow the result set.
    """
    q = query.replace("×", "х").replace("*", "х").replace("x", "х").replace("X", "х")
    raw: list[str] = []
    for word in q.split():
        parts = [p for p in word.split("х") if p]
        raw.extend(parts if len(parts) > 1 else [word])

    required, optional = [], []
    for w in raw:
        f = _fold(w.strip(".,;:()/"))
        if len(f) < 2 or f in _STOPWORDS:
            continue
        if any(ch.isdigit() for ch in f):
            required.append(f)
        elif f not in optional:
            optional.append(f)
    if not required and optional:
        longest = max(optional, key=len)
        required = [longest]
        optional = [o for o in optional if o != longest]
    return required, optional


# Product-class synonym groups (Ukrainian/Russian stems). Used to rank rows of the
# SAME class as the query first: "болт М6" must rank actual bolts above nuts/rivets
# that merely contain "М6".
_CATEGORY_GROUPS = [
    ["болт", "гвинт", "винт"],
    ["гайк"],
    ["шайб"],
    ["заклеп", "заклёп"],
    ["шпильк"],
    ["заклепочн", "заклепувальн", "заклёпочн", "клепальник"],
    ["саморіз", "саморез"],
    ["анкер"],
    ["дюбел"],
    ["шуруп"],
]


def _category_patterns(query: str) -> list[str]:
    """Folded name-substrings to boost rows of the same product class as the query."""
    q = _fold(query)
    for group in _CATEGORY_GROUPS:
        if any(_fold(stem) in q for stem in group):
            return [_fold(stem) for stem in group]
    return []


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
            # Fastener names scatter attributes ("Гайка Din 6923 M8 цб зубч") and mix
            # Latin/Cyrillic, so requiring every word — including descriptors like
            # "з фланцем" — misses real products. Instead: require only the selective
            # digit tokens (sizes/standards/dims), match script-insensitively, and
            # rank by how many descriptive words also hit.
            required, optional = _tokenize_query(query)

            params: list = [f"%{query}%"]  # $1 = raw phrase (code match)
            req_conds = []
            for t in required:
                params.append(f"%{t}%")
                req_conds.append(f"{_NAME_FOLD_SQL} LIKE ${len(params)}")
            req_clause = " AND ".join(req_conds) if req_conds else "name ILIKE $1"

            score_terms = []
            for t in optional:
                params.append(f"%{t}%")
                score_terms.append(
                    f"(CASE WHEN {_NAME_FOLD_SQL} LIKE ${len(params)} THEN 1 ELSE 0 END)"
                )
            score_sql = " + ".join(score_terms) if score_terms else "0"

            # Same-class boost: rank rows whose name is the queried product type first.
            cat_conds = []
            for p in _category_patterns(query):
                params.append(f"%{p}%")
                cat_conds.append(f"{_NAME_FOLD_SQL} LIKE ${len(params)}")
            cat_boost = (
                f"CASE WHEN ({' OR '.join(cat_conds)}) THEN 0 ELSE 1 END"
                if cat_conds else "1"
            )

            sql = f"""
                SELECT ref_key, name, code, price, stock, ({score_sql}) AS score
                FROM products
                WHERE deleted = false
                  AND ( code ILIKE $1 OR ({req_clause}) )
                ORDER BY
                  CASE WHEN code ILIKE $1 THEN 0 ELSE 1 END,
                  {cat_boost},
                  (stock > 0) DESC,
                  score DESC,
                  (price > 0) DESC,
                  stock DESC
                LIMIT 8
            """
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
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

    # Fallback: direct OData — Артикул is the article field (not Код); no price/stock in catalog
    url_article = f"{BAS_URL}/Catalog_Номенклатура?$filter=Артикул eq '{query}'&$format=json"
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
                        "article": i.get("Артикул", ""),
                        "name": i.get("Description", ""),
                        "price": 0,
                        "stock": 0,
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

    # Check PG first — covers locally-created AI orders and synced BAS orders
    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ref_key, number, date, amount FROM orders "
                    "WHERE ref_key = $1 OR number = $2 LIMIT 1",
                    order_id, order_id,
                )
            if row:
                is_ai = str(row["number"]).startswith("AI-")
                return {
                    "id": row["ref_key"],
                    "number": row["number"],
                    "status": "pending_confirmation" if is_ai else "confirmed",
                    "status_label": "Очікує підтвердження менеджером" if is_ai else "Підтверджено в БАС",
                    "date": str(row["date"])[:10] if row["date"] else "",
                    "amount": float(row["amount"] or 0),
                }
        except Exception as e:
            logger.error(f"[PG] get_order_status error: {e}")

    # AI-prefixed orders are local-only — OData won't have them
    if order_id.startswith("AI-"):
        return None

    # Fallback: OData (only works for BAS-native orders by guid)
    url = f"{BAS_URL}/Document_ЗаказПокупателя(guid'{order_id}')?$format=json"
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(_url(url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                if not data:
                    return None
                return {
                    "id": data.get("Ref_Key", order_id),
                    "status": data.get("Статус", "confirmed"),
                    "status_label": data.get("СтатусНаименование", "В системі"),
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

    logger.info(f"check_supplier: escalating to manager for '{product_name}' qty={qty}")
    return {
        "available": None,
        "qty": 0,
        "lead_days": None,
        "note": (
            "Онлайн перевірка у постачальника недоступна. "
            "Передай запит менеджеру через notify_manager — він уточнить наявність та терміни."
        ),
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
    import uuid
    from datetime import date

    def _num(v) -> float:
        """Coerce possibly-None / string values to float safely."""
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    total = sum(_num(i.get("price")) * _num(i.get("qty")) for i in items)

    if USE_MOCK:
        order_id = f"order_mock_{client_id[:8]}"
        logger.info(f"[MOCK] create_order for {client_name} -> {order_id}, total={total}")
        return {"success": True, "order_id": order_id, "total": total}

    # Save to local PG (BAS OData rejects POST due to server-side BeforeWrite handler)
    local_id = str(uuid.uuid4())
    local_number = f"AI-{local_id[:8].upper()}"

    pool = _get_pool()
    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO orders (ref_key, number, date, client_ref_key, amount)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (ref_key) DO NOTHING
                    """,
                    local_id, local_number, date.today(), client_id, total,
                )
            logger.info(f"[ORDER] Saved locally {local_number} total={total}")
        except Exception as e:
            logger.error(f"[ORDER] PG save error: {e}")

    items_str = "; ".join(
        f"{i.get('name', i.get('article', '?'))} × {i.get('qty')} шт × {i.get('price')} грн"
        for i in items
    )
    logger.info(
        f"[ORDER] New order {local_number}: client={client_name} phone={client_phone} "
        f"city={city} total={total} items=[{items_str}] comment={comment}"
    )

    return {
        "success": True,
        "order_id": local_number,
        "total": total,
        "note": "Заказ сохранён. Менеджер свяжется для подтверждения.",
    }
