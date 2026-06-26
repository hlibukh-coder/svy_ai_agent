"""OData client for BAS (1C-based) — pulls raw data via HTTP."""
import logging
import os
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BAS_URL = os.getenv("BAS_URL", "")
BAS_LOGIN = os.getenv("BAS_LOGIN", os.getenv("BAS_USER", ""))
BAS_PASSWORD = os.getenv("BAS_PASSWORD", os.getenv("BAS_PASS", ""))


class BASClient:
    def __init__(
        self,
        base_url: str = BAS_URL,
        login: str = BAS_LOGIN,
        password: str = BAS_PASSWORD,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = (login, password)

    async def _get(self, entity: str, params: dict | None = None) -> list[dict]:
        url = f"{self.base_url}/{entity}"
        query = {"$format": "json", **(params or {})}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, params=query, auth=self.auth)
                r.raise_for_status()
                return r.json().get("value", [])
        except Exception as e:
            logger.error(f"[BAS OData] GET {entity} error: {e}")
            return []

    async def get_products(self, since: datetime | None = None) -> list[dict]:
        # Артикул = article/SKU (the "index"); Code = the 1C internal Код
        # (e.g. "НФ-00000670"). Both are distinct identifiers customers search by.
        # No price/stock in catalog.
        params: dict[str, Any] = {
            "$select": "Ref_Key,Description,Артикул,Code,DeletionMark"
        }
        if since:
            params["$filter"] = f"Timestamp gt datetime'{since.strftime('%Y-%m-%dT%H:%M:%S')}'"
        return await self._get("Catalog_Номенклатура", params)

    async def get_prices(self) -> list[dict]:
        return await self._get(
            "InformationRegister_ЦеныНоменклатуры",
            {"$select": "Номенклатура_Key,Цена"},
        )

    async def get_clients(self, since: datetime | None = None) -> list[dict]:
        # НомерТелефонаДляПоиска = phone for search (not НомерТелефона)
        # Code = client code (not Код); no Город field in BAS
        params: dict[str, Any] = {
            "$select": "Ref_Key,Description,Code,НомерТелефонаДляПоиска,НаименованиеПолное,DeletionMark"
        }
        if since:
            params["$filter"] = f"Timestamp gt datetime'{since.strftime('%Y-%m-%dT%H:%M:%S')}'"
        return await self._get("Catalog_Контрагенты", params)

    async def get_orders(self, since: datetime | None = None) -> list[dict]:
        # Real BAS field names: Posted, Number (Ukrainian BAS uses English API names)
        # Expand of tabular sections is not supported by this BAS OData endpoint
        params: dict[str, Any] = {
            "$select": "Ref_Key,Date,Контрагент_Key,СуммаДокумента,Posted,Number",
        }
        if since:
            params["$filter"] = f"Date gt datetime'{since.strftime('%Y-%m-%dT%H:%M:%S')}'"
        else:
            params["$orderby"] = "Date desc"
            params["$top"] = "5000"
        return await self._get("Document_ЗаказПокупателя", params)

    async def get_stock(self) -> list[dict]:
        # Raw movement records: Количество + RecordType (Receipt/Expense) per Номенклатура_Key
        # Balance virtual tables are unavailable; stock is calculated from movements on our side
        return await self._get(
            "AccumulationRegister_ЗапасыНаСкладах",
            {"$top": "100000"},
        )

    async def get_order_items(self, order_id: str) -> list[dict]:
        return await self._get(
            f"Document_ЗаказПокупателя(guid'{order_id}')/Товары",
            {"$select": "Номенклатура_Key,Количество,Цена,Сумма"},
        )
