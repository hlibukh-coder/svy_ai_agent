import json
import logging
import os
from src import bas

logger = logging.getLogger(__name__)

ESCALATION_CHAT_ID = os.getenv("ESCALATION_CHAT_ID", "")

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_products",
            "description": "Найти товар по названию или артикулу. Возвращает цену и остаток.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Название товара или артикул",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_client",
            "description": "Найти клиента в BAS по номеру телефона.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Номер телефона клиента в формате +380XXXXXXXXX",
                    }
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": "История заказов клиента, последние 10.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id": {
                        "type": "string",
                        "description": "ID клиента из BAS",
                    }
                },
                "required": ["client_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_order",
            "description": "Создать заказ в BAS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id":    {"type": "string", "description": "ID клиента"},
                    "client_name":  {"type": "string", "description": "Имя клиента"},
                    "client_phone": {"type": "string", "description": "Телефон клиента"},
                    "company":      {"type": "string", "description": "Название компании (для юрлица)"},
                    "city":         {"type": "string", "description": "Город доставки"},
                    "items": {
                        "type": "array",
                        "description": "Список товаров",
                        "items": {
                            "type": "object",
                            "properties": {
                                "article": {"type": "string"},
                                "name":    {"type": "string"},
                                "qty":     {"type": "integer"},
                                "price":   {"type": "number"},
                            },
                            "required": ["name", "qty"],
                        },
                    },
                    "comment": {"type": "string", "description": "Комментарий к заказу"},
                },
                "required": ["client_id", "client_name", "client_phone", "city", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Получить статус существующего заказа по его ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "ID заказа из BAS",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_supplier",
            "description": "Уточнить наличие товара у поставщика, если на складе нет нужного количества.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Название товара",
                    },
                    "qty": {
                        "type": "integer",
                        "description": "Нужное количество",
                    },
                },
                "required": ["product_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_manager",
            "description": (
                "Передать диалог живому менеджеру. Вызывать при: жалобе, запросе скидки/отсрочки, "
                "вопросе по существующему заказу, просьбе поговорить с человеком."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "client_request",
                            "complaint",
                            "discount_request",
                            "complex_question",
                            "order_issue",
                        ],
                        "description": "Причина эскалации",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Краткое описание ситуации для менеджера",
                    },
                },
                "required": ["reason", "summary"],
            },
        },
    },
]

# Will be set from index.py after tg client is ready
_tg_client = None
_escalation_peer = None


def set_tg_client(client, escalation_peer=None):
    global _tg_client, _escalation_peer
    _tg_client = client
    _escalation_peer = escalation_peer


async def execute_tool(name: str, arguments: dict, sender_phone: str = "") -> str:
    logger.info(f"[TOOL] {name}({json.dumps(arguments, ensure_ascii=False)})")

    if name == "get_products":
        products = await bas.get_products(arguments["query"])
        if not products:
            return json.dumps({"result": "Товар не найден"}, ensure_ascii=False)
        return json.dumps({"products": products}, ensure_ascii=False)

    elif name == "get_client":
        client = await bas.get_client(arguments["phone"])
        if not client:
            return json.dumps({"result": "Клиент не найден"}, ensure_ascii=False)
        return json.dumps({"client": client}, ensure_ascii=False)

    elif name == "get_orders":
        orders = await bas.get_orders(arguments["client_id"])
        return json.dumps({"orders": orders}, ensure_ascii=False)

    elif name == "create_order":
        result = await bas.create_order(
            client_id=arguments.get("client_id", ""),
            client_name=arguments.get("client_name", ""),
            client_phone=arguments.get("client_phone", sender_phone),
            company=arguments.get("company", ""),
            city=arguments.get("city", ""),
            items=arguments.get("items", []),
            comment=arguments.get("comment", ""),
        )
        return json.dumps(result, ensure_ascii=False)

    elif name == "get_order_status":
        result = await bas.get_order_status(arguments["order_id"])
        if not result:
            return json.dumps({"result": "Заказ не найден"}, ensure_ascii=False)
        return json.dumps({"order": result}, ensure_ascii=False)

    elif name == "check_supplier":
        result = await bas.check_supplier(
            arguments["product_name"],
            arguments.get("qty", 1),
        )
        return json.dumps(result, ensure_ascii=False)

    elif name == "notify_manager":
        reason = arguments.get("reason", "")
        summary = arguments.get("summary", "")
        await _send_escalation(reason, summary, sender_phone)
        return json.dumps({"result": "Менеджер уведомлён"}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)


async def _send_escalation(reason: str, summary: str, sender_phone: str):
    if not _tg_client or not _escalation_peer:
        logger.warning(f"[ESCALATION] No tg client or peer. reason={reason}, summary={summary}")
        return
    text = (
        f"🚨 Эскалация\n"
        f"Причина: {reason}\n"
        f"Телефон: {sender_phone}\n"
        f"Суть: {summary}"
    )
    try:
        await _tg_client.send_message(_escalation_peer, text)
        logger.info(f"[ESCALATION] Sent to {_escalation_peer}")
    except Exception as e:
        logger.error(f"[ESCALATION] Failed: {e}")
