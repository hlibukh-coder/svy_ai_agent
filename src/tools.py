import json
import logging
import mimetypes
import os
from src import bas, config
from src import accounts as account_manager

logger = logging.getLogger(__name__)

ESCALATION_CHAT_ID = os.getenv("ESCALATION_CHAT_ID", "")
DOCS_DIR = os.getenv("DOCS_DIR", "docs")

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_products",
            "description": (
                "Найти товар по НАЗВАНИЮ или по АРТИКУЛУ/КОДУ. Если клиент называет код/"
                "артикул (напр. 'DIN 933', 'DIN-934', '12345') — ищи по нему: поиск "
                "нормализует пробелы и дефисы и ставит точное совпадение по коду первым. "
                "Возвращает название, код/артикул, цену и остаток."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Название товара ИЛИ артикул/код (передавай код как есть)",
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
                            "order_created",
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
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": (
                "Отправить клиенту файл в текущий чат: прайс-лист, карточку/паспорт товара "
                "или счёт по последнему заказу. Используй doc_id из доступного каталога "
                "(напр. 'pricelist', 'datasheet_<артикул>') или 'invoice' для счёта."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "Идентификатор документа из каталога docs/ или 'invoice'",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Короткая подпись к файлу (необязательно)",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_offer",
            "description": (
                "Сформировать коммерческое предложение (КП) в PDF и отправить клиенту в "
                "текущий чат. Используй, когда клиент готов получить КП или оператор дал "
                "команду «выстави КП». Для каждой позиции укажи название ИЛИ артикул, "
                "количество и (если задана) цену; если цену не указать — берётся из "
                "каталога сайта/BAS. Сумму считать НЕ нужно — она считается сама."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_name":  {"type": "string", "description": "Имя/название клиента"},
                    "client_phone": {"type": "string", "description": "Телефон клиента"},
                    "company":      {"type": "string", "description": "Компания (для юрлица, необязательно)"},
                    "items": {
                        "type": "array",
                        "description": "Позиции КП",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":    {"type": "string", "description": "Название товара"},
                                "article": {"type": "string", "description": "Артикул/код (vendorCode)"},
                                "qty":     {"type": "number", "description": "Количество"},
                                "price":   {"type": "number", "description": "Цена за шт, грн (если задана оператором)"},
                            },
                            "required": ["qty"],
                        },
                    },
                    "comment": {"type": "string", "description": "Комментарий в КП (условия, сроки)"},
                    "caption": {"type": "string", "description": "Подпись к файлу в мессенджере (необязательно)"},
                },
                "required": ["items"],
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


async def execute_tool(name: str, arguments: dict, sender_phone: str = "", conv: dict | None = None) -> str:
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
        _ch = (conv or {}).get("channel", "telegram")
        _acc = (conv or {}).get("account_id")
        result = await bas.create_order(
            client_id=arguments.get("client_id", ""),
            client_name=arguments.get("client_name", ""),
            client_phone=arguments.get("client_phone", sender_phone),
            company=arguments.get("company", ""),
            city=arguments.get("city", ""),
            items=arguments.get("items", []),
            comment=arguments.get("comment", ""),
            channel=_ch,
            account_id=_acc,
        )
        if result.get("success"):
            items_lines = "\n".join(
                "  • " + (i.get("name") or i.get("article", "?"))
                + f" × {i.get('qty')} шт"
                + (f" × {i.get('price')} грн" if i.get("price") else "")
                for i in arguments.get("items", [])
            )
            summary = (
                f"📦 Нове замовлення {result['order_id']}\n"
                f"Клієнт: {arguments.get('client_name', '—')} "
                f"{arguments.get('client_phone', sender_phone)}\n"
            )
            if arguments.get("company"):
                summary += f"Компанія: {arguments['company']}\n"
            summary += f"Місто: {arguments.get('city', '—')}\n"
            if items_lines:
                summary += f"Товари:\n{items_lines}\n"
            summary += f"Сума: {result.get('total', 0)} грн"
            if arguments.get("comment"):
                summary += f"\nКомент: {arguments['comment']}"
            await _send_escalation("order_created", summary, sender_phone, conv=conv)
            await config.log_event(
                "order_created",
                f"Створено заказ {result.get('order_id', '')} для {arguments.get('client_name', '—')}",
                {"total": result.get("total", 0), "phone": arguments.get("client_phone", sender_phone),
                 "channel": _ch, "account_id": _acc},
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
        await _send_escalation(reason, summary, sender_phone, conv=conv)
        await config.log_event("escalation", f"Передано менеджеру: {summary[:60]}",
                               {"reason": reason, "phone": sender_phone,
                                "channel": (conv or {}).get("channel", "telegram"),
                                "account_id": (conv or {}).get("account_id")})
        return json.dumps({"result": "Менеджер уведомлён"}, ensure_ascii=False)

    elif name == "send_file":
        return await _handle_send_file(arguments, conv)

    elif name == "create_offer":
        return await _handle_create_offer(arguments, sender_phone, conv)

    return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)


async def _handle_create_offer(arguments: dict, sender_phone: str, conv: dict | None) -> str:
    """Build a КП PDF and send it to the client in the current chat."""
    from src import offer
    if not conv or not conv.get("conv_id"):
        return json.dumps({"ok": False, "error": "Нет контекста чата для отправки КП"},
                          ensure_ascii=False)
    items = arguments.get("items") or []
    if not items:
        return json.dumps({"ok": False, "error": "Не указаны позиции для КП"}, ensure_ascii=False)
    try:
        doc = await offer.build_offer_doc(
            client_name=arguments.get("client_name", "") or (conv or {}).get("client_name", ""),
            client_phone=arguments.get("client_phone", sender_phone) or sender_phone,
            company=arguments.get("company", ""),
            items=items,
            comment=arguments.get("comment", ""),
        )
    except Exception as e:
        logger.error(f"[OFFER] build failed: {e}")
        return json.dumps({"ok": False, "error": f"Не удалось собрать КП: {e}"}, ensure_ascii=False)

    caption = arguments.get("caption") or f"Комерційна пропозиція {doc['offer_no']}"
    res = await _send_doc(conv, doc, caption)
    if res.get("ok"):
        await config.log_event(
            "offer_sent",
            f"Відправлено КП {doc['offer_no']} ({offer._fmt(doc['total'])} грн)",
            {"offer_no": doc["offer_no"], "total": doc["total"],
             "channel": conv.get("channel"), "account_id": conv.get("account_id")},
        )
        return json.dumps({"ok": True, "offer_no": doc["offer_no"], "total": doc["total"],
                           "lines": doc["lines"]}, ensure_ascii=False)
    return json.dumps({"ok": False, "error": res.get("error", "send failed"),
                       "offer_no": doc["offer_no"]}, ensure_ascii=False)


async def _send_doc(conv: dict, doc: dict, caption: str = "") -> dict:
    """Send an in-memory/file doc through the conversation's channel adapter."""
    from src.channels import registry
    adapter = registry.get_by_conv(conv["conv_id"])
    if adapter is None:
        return {"ok": False, "error": "Канал недоступен"}
    src = doc["src"]
    size = len(src) if isinstance(src, (bytes, bytearray)) else (
        os.path.getsize(src) if isinstance(src, str) and os.path.exists(src) else 0)
    if size and size > adapter.max_file_bytes():
        return {"ok": False, "error": "Файл слишком большой для этого канала"}
    res = await adapter.send_file(
        conv["peer"], src, caption=caption,
        filename=doc["filename"], mimetype=doc["mimetype"],
    )
    return {"ok": res.ok, "error": res.error}


# ── file sending (AI tool + dashboard operator) ──────────────────────────────
async def _handle_send_file(arguments: dict, conv: dict | None) -> str:
    if not conv or not conv.get("conv_id"):
        return json.dumps({"ok": False, "error": "Нет контекста чата для отправки файла"},
                          ensure_ascii=False)
    from src.channels import registry
    adapter = registry.get_by_conv(conv["conv_id"])
    if adapter is None:
        return json.dumps({"ok": False, "error": "Канал недоступен"}, ensure_ascii=False)
    doc = await _resolve_doc(arguments.get("doc_id", ""), conv)
    if doc is None:
        return json.dumps({"ok": False, "error": "Документ не найден"}, ensure_ascii=False)
    src = doc["src"]
    size = len(src) if isinstance(src, (bytes, bytearray)) else (
        os.path.getsize(src) if isinstance(src, str) and os.path.exists(src) else 0)
    if size and size > adapter.max_file_bytes():
        return json.dumps({"ok": False, "error": "Файл слишком большой для этого канала"},
                          ensure_ascii=False)
    res = await adapter.send_file(
        conv["peer"], src, caption=arguments.get("caption", ""),
        filename=doc["filename"], mimetype=doc["mimetype"],
    )
    if res.ok:
        await config.log_event("file_sent", f"Відправлено файл: {doc['filename']}",
                               {"channel": conv.get("channel"), "account_id": conv.get("account_id")})
    return json.dumps({"ok": res.ok, "error": res.error}, ensure_ascii=False)


def _find_doc_file(docs_dir: str, doc_id: str) -> str | None:
    """Resolve a doc_id to a real file under docs/ — exact, with common extensions,
    or case-insensitive stem match."""
    if not doc_id or not os.path.isdir(docs_dir):
        return None
    exact = os.path.join(docs_dir, doc_id)
    if os.path.isfile(exact):
        return exact
    for ext in (".pdf", ".txt", ".jpg", ".jpeg", ".png", ".xlsx", ".docx", ".csv"):
        p = os.path.join(docs_dir, doc_id + ext)
        if os.path.isfile(p):
            return p
    target = doc_id.lower()
    try:
        for fn in os.listdir(docs_dir):
            stem = os.path.splitext(fn)[0].lower()
            if stem == target or target in stem:
                return os.path.join(docs_dir, fn)
    except OSError:
        pass
    return None


async def _generate_invoice(conv: dict) -> dict | None:
    """Render a simple text invoice for the conversation's client from their last order."""
    phone = (conv or {}).get("phone", "")
    client = await bas.get_client(phone) if phone else None
    if not client:
        return None
    orders = await bas.get_orders(client.get("id", ""))
    if not orders:
        return None
    last = orders[0]
    lines = [
        "РАХУНОК-ФАКТУРА (попередній)",
        f"Клієнт: {client.get('name', '—')}",
        f"Телефон: {phone or '—'}",
        f"Замовлення № {last.get('number', '—')} від {last.get('date', '—')}",
        "",
    ]
    for it in last.get("items", []):
        lines.append(f"  • {it.get('name', '?')} — {it.get('qty', '?')} шт")
    lines += ["", f"Сума: {last.get('total', 0)} грн",
              "", "Це попередній рахунок. Остаточний підтвердить менеджер."]
    data = ("\n".join(lines)).encode("utf-8")
    return {"src": data, "filename": f"invoice_{last.get('number', 'order')}.txt",
            "mimetype": "text/plain"}


async def _resolve_doc(doc_id: str, conv: dict | None) -> dict | None:
    doc_id = (doc_id or "").strip()
    if doc_id.lower() in ("invoice", "рахунок", "счет", "счёт"):
        return await _generate_invoice(conv or {})
    path = _find_doc_file(DOCS_DIR, doc_id)
    if path:
        mt, _ = mimetypes.guess_type(path)
        return {"src": path, "filename": os.path.basename(path),
                "mimetype": mt or "application/octet-stream"}
    return None


async def _send_escalation(reason: str, summary: str, sender_phone: str, conv: dict | None = None):
    channel = (conv or {}).get("channel", "telegram")
    account_id = (conv or {}).get("account_id")
    text = (
        f"🚨 Передано менеджеру\n"
        f"Канал: {channel}" + (f" (акаунт #{account_id})" if account_id else "") + "\n"
        f"Причина: {reason}\n"
        f"Телефон: {sender_phone or '—'}\n"
        f"Суть: {summary}"
    )

    # 1) Per-account escalation peer (credentials.escalation_peer) → send via that
    #    account's own adapter, so each business can route hand-offs where it wants.
    if account_id is not None:
        try:
            acct = await account_manager.get_account(account_id, include_secrets=True)
            esc_peer = (acct or {}).get("credentials", {}).get("escalation_peer") if acct else None
            if esc_peer:
                from src.channels import registry
                adapter = registry.get(channel, account_id)
                if adapter is not None:
                    res = await adapter.send_text(str(esc_peer), text)
                    if res.ok:
                        logger.info(f"[ESCALATION] sent via {channel}:{account_id} -> {esc_peer}")
                        return
        except Exception as e:
            logger.error(f"[ESCALATION] per-account route failed: {e}")

    # 2) Fallback: the default Telegram manager chat (ESCALATION_CHAT_ID) or, if unset,
    #    the operator's own Saved Messages ("me") — a hand-off is NEVER lost.
    if not _tg_client:
        logger.warning(f"[ESCALATION] No tg client. reason={reason}, summary={summary}")
        return
    peer = _escalation_peer or "me"
    try:
        m = await _tg_client.send_message(peer, text)
        from src.channels.telegram_adapter import mark_sent
        mark_sent(m)  # escalation ping — not a phone-typed client message
        logger.info(f"[ESCALATION] Sent to {peer}")
    except Exception as e:
        logger.error(f"[ESCALATION] Failed: {e}")
