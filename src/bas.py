import os
import logging
import aiohttp
from src.prompt import MOCK_CLIENTS, MOCK_PRODUCTS

logger = logging.getLogger(__name__)

USE_MOCK = os.getenv("USE_MOCK", "true").lower() == "true"
BAS_URL = os.getenv("BAS_URL", "")
BAS_USER = os.getenv("BAS_USER", "")
BAS_PASS = os.getenv("BAS_PASS", "")


def _auth():
    return aiohttp.BasicAuth(BAS_USER, BAS_PASS)


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

    url = f"{BAS_URL}/Catalog_Контрагенты?$filter=contains(НомерТелефона,'{phone}')&$top=1&$format=json"
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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


async def get_orders(client_id: str) -> list:
    if USE_MOCK:
        for client in MOCK_CLIENTS.values():
            if client["id"] == client_id:
                logger.info(f"[MOCK] get_orders({client_id}) -> {len(client['orders'])} orders")
                return client["orders"]
        return []

    url = (
        f"{BAS_URL}/Document_ЗаказПокупателя"
        f"?$filter=Контрагент_Key eq guid'{client_id}'&$orderby=Date desc&$top=10&$format=json"
    )
    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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


async def get_products(query: str) -> list:
    if USE_MOCK:
        q = query.lower()
        results = [
            p for p in MOCK_PRODUCTS
            if q in p["name"].lower() or q in p["article"].lower()
        ]
        logger.info(f"[MOCK] get_products('{query}') -> {len(results)} results")
        return results

    # Try by article first
    url_article = f"{BAS_URL}/Catalog_Номенклатура?$filter=Код eq '{query}'&$format=json"
    url_name = f"{BAS_URL}/Catalog_Номенклатура?$filter=contains(Description,'{query}')&$top=5&$format=json"

    try:
        async with aiohttp.ClientSession(auth=_auth()) as session:
            async with session.get(url_article, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                items = data.get("value", [])
                if not items:
                    async with session.get(url_name, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
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
