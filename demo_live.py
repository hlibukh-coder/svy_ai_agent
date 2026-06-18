"""
demo_live.py - live demo of the AI agent.

Run:
    .venv\Scripts\python.exe demo_live.py
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# Set USE_MOCK=false before importing src modules
os.environ["USE_MOCK"] = "false"

SEP = "-" * 60

def hdr(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")

def ok(label, value):
    print(f"  [OK]  {label}: {value}")

def info(msg):
    print(f"  [..]  {msg}")

def err(msg):
    print(f"  [ERR] {msg}", file=sys.stderr)


# ============================================================
# BLOK 1 - direct OData request via httpx (no agent)
# ============================================================
async def demo_odata_direct():
    hdr("BLOK 1 - Direct OData request to BAS (httpx)")

    import httpx
    bas_url = os.getenv("BAS_URL", "").rstrip("/")
    login   = os.getenv("BAS_LOGIN", os.getenv("BAS_USER", ""))
    pw      = os.getenv("BAS_PASSWORD", os.getenv("BAS_PASS", ""))

    if not bas_url or not login:
        err("BAS_URL / BAS_LOGIN not set in .env - skipping")
        return

    nomenklatura = "Catalog_\u041d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430"
    url = f"{bas_url}/{nomenklatura}?$top=5&$format=json&$select=Description,DeletionMark"
    info(f"URL: {url}")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, auth=(login, pw))
        r.raise_for_status()
        items = r.json().get("value", [])
        ok("HTTP status", r.status_code)
        ok("Products received", len(items))
        for i, item in enumerate(items[:3], 1):
            name = item.get("Description", "")
            print(f"      [{i}] {name}")
    except Exception as e:
        err(f"OData error: {e}")


# ============================================================
# BLOK 2 - agent tools (src/bas.py)
# ============================================================
async def demo_agent_tools():
    hdr("BLOK 2 - Agent tools (src/bas.py OData fallback)")

    import src.bas as bas
    # Ensure live mode
    bas.USE_MOCK = False

    # get_products
    query = "\u0431\u043e\u043b\u0442"
    info(f"get_products('{query}')")
    try:
        products = await bas.get_products(query)
        ok("get_products", f"{len(products)} items")
        for p in products[:3]:
            print(f"      {p.get('article','')} | {p.get('name','')} | {p.get('price','')} grn | stock={p.get('stock','')}")
        if not products:
            info("No products found (no PG, BAS field names may differ)")
    except Exception as e:
        err(f"get_products: {e}")

    # get_client - get real phone from BAS
    import httpx
    bas_url = os.getenv("BAS_URL", "").rstrip("/")
    login   = os.getenv("BAS_LOGIN", os.getenv("BAS_USER", ""))
    pw      = os.getenv("BAS_PASSWORD", os.getenv("BAS_PASS", ""))

    phone_to_test = None
    try:
        tel_field = "\u041d\u043e\u043c\u0435\u0440\u0422\u0435\u043b\u0435\u0444\u043e\u043d\u0430"
        cl_url = (
            f"{bas_url}/Catalog_\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442\u044b"
            f"?$top=10&$format=json&$select=Description,{tel_field}"
        )
        async with httpx.AsyncClient(timeout=15) as hclient:
            r = await hclient.get(cl_url, auth=(login, pw))
            r.raise_for_status()
            clients_raw = r.json().get("value", [])
            for c in clients_raw:
                p = (c.get(tel_field) or "").strip()
                if p:
                    phone_to_test = p
                    info(f"Phone from BAS: {phone_to_test}")
                    break
    except Exception as e:
        info(f"Could not get phone from OData: {e}")

    if phone_to_test:
        try:
            client = await bas.get_client(phone_to_test)
            if client:
                ok("get_client", f"id={client.get('id','')} name={client.get('name','')}")
                orders = await bas.get_orders(client["id"])
                ok("get_orders", f"{len(orders)} orders")
                for o in orders[:2]:
                    print(f"      #{o.get('number','')} {o.get('date','')} {o.get('total','')} grn")
            else:
                info("Client not found via get_client()")
        except Exception as e:
            err(f"get_client/get_orders: {e}")
    else:
        info("No phone retrieved - get_client skipped")


# ============================================================
# BLOK 3 - GPT-4o + function calling
# ============================================================
async def demo_agent_response():
    hdr("BLOK 3 - Agent generates response (GPT-4o + tools)")

    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        err("OPENAI_API_KEY not set - skipping")
        return

    from openai import AsyncOpenAI
    from src.prompt import build_system_prompt
    from src import tools
    import src.bas as bas
    bas.USE_MOCK = False

    openai_client = AsyncOpenAI(api_key=openai_key)

    user_message = (
        "\u0414\u043e\u0431\u0440\u0438\u0439 \u0434\u0435\u043d\u044c! "
        "\u0404 \u0431\u043e\u043b\u0442\u0438 \u041c8? "
        "\u0421\u043a\u0456\u043b\u044c\u043a\u0438 \u043a\u043e\u0448\u0442\u0443\u044e\u0442\u044c "
        "\u0456 \u0441\u043a\u0456\u043b\u044c\u043a\u0438 \u0454 \u0432 \u043d\u0430\u044f\u0432\u043d\u043e\u0441\u0442\u0456?"
    )
    sender_phone = "+380681234567"

    info(f"Incoming message: {user_message}")
    info(f"Phone: {sender_phone}")

    client_data = await bas.get_client(sender_phone)
    orders = []
    if client_data:
        orders = await bas.get_orders(client_data["id"])
        info(f"Client from BAS: {client_data.get('name')} / {len(orders)} orders")
    else:
        info("Client not found - agent will reply without personalization")

    system_prompt = build_system_prompt(client_data, orders)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    for iteration in range(5):
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools.TOOLS_SCHEMA,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            ok("Agent reply", "")
            print(f"\n  >> {msg.content}\n")
            return

        messages.append(msg)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}
            info(f"Agent called tool: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")
            result = await tools.execute_tool(fn_name, fn_args, sender_phone)
            info(f"  -> result: {result[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
    )
    ok("Agent reply (final)", "")
    print(f"\n  >> {response.choices[0].message.content}\n")


# ============================================================
# BLOK 4 - outbound: agent writes to client first
# ============================================================
async def demo_outbound():
    hdr("BLOK 4 - Outbound: agent writes to client first")

    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        err("OPENAI_API_KEY not set - skipping")
        return

    from openai import AsyncOpenAI
    from src.prompt import build_system_prompt
    import src.bas as bas

    openai_client = AsyncOpenAI(api_key=openai_key)

    # Use mock client data for outbound demo (no real send)
    bas.USE_MOCK = True
    sender_phone = "+380681234567"
    client_data  = await bas.get_client(sender_phone)
    orders       = await bas.get_orders(client_data["id"]) if client_data else []
    bas.USE_MOCK = False

    name = client_data.get("name", "Client") if client_data else "Client"
    last_order = orders[0] if orders else None
    last_items = (last_order.get("items") or []) if last_order else []
    last_item  = last_items[0] if last_items else {}
    item_name  = last_item.get("name", "\u0431\u043e\u043b\u0442\u0438")

    system_prompt = build_system_prompt(client_data, orders)
    task = (
        f"\u041d\u0430\u043f\u0438\u0448\u0438 \u043a\u043b\u0456\u0454\u043d\u0442\u0443 {name} "
        f"\u043a\u043e\u0440\u043e\u0442\u043a\u0435 outbound-\u043f\u043e\u0432\u0456\u0434\u043e\u043c\u043b\u0435\u043d\u043d\u044f (1-3 \u0440\u0435\u0447\u0435\u043d\u043d\u044f). "
        f"\u0412\u0456\u043d \u0437\u0430\u0437\u0432\u0438\u0447\u0430\u0439 \u043a\u0443\u043f\u0443\u0454: {item_name}. "
        f"\u041d\u0430\u0433\u0430\u0434\u0430\u0439 \u043f\u0440\u043e \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0435 \u0437\u0430\u043c\u043e\u0432\u043b\u0435\u043d\u043d\u044f. "
        f"\u041f\u0438\u0448\u0438 \u043d\u0430 '\u0412\u0438', \u0443\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u043e\u044e."
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": task},
        ],
    )
    outbound_text = response.choices[0].message.content or ""

    info(f"Client: {name} ({sender_phone})")
    info("Generated outbound message:")
    print(f"\n  [OUT] {outbound_text}\n")
    info("(real send via Telegram - only when tg-client is running)")


# ============================================================
async def main():
    print("\n" + "=" * 60)
    print("  SVY AI AGENT - LIVE DEMO")
    print("=" * 60)

    await demo_odata_direct()
    await demo_agent_tools()
    await demo_agent_response()
    await demo_outbound()

    hdr("DONE")
    print("  All blocks completed.\n")


if __name__ == "__main__":
    asyncio.run(main())
