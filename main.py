import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.index import send_to_client

logger = logging.getLogger(__name__)

app = FastAPI()

USE_MOCK = os.getenv("USE_MOCK", "true").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", "")


class SendRequest(BaseModel):
    phone: str
    text: str


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


@app.post("/send")
async def send_message(req: SendRequest):
    """Send an outbound message to a client by phone number.
    The agent generates a reply via OpenAI and sends it via Telegram.
    """
    if not req.phone or not req.text:
        raise HTTPException(status_code=400, detail="phone and text are required")
    result = await send_to_client(req.phone, req.text)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result


@app.on_event("startup")
async def startup():
    """Initialize PG pool and start BAS sync scheduler on startup."""
    if USE_MOCK or not DATABASE_URL:
        logger.info("[STARTUP] USE_MOCK=true or no DATABASE_URL — skipping PG sync")
        return

    try:
        import asyncpg
        from sync.client import BASClient
        from sync import scheduler_sync

        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        bas_client = BASClient()

        # Start scheduler (periodic sync every SYNC_INTERVAL_MINUTES)
        await scheduler_sync.init(pool, bas_client)

        # Run initial full sync in background so startup doesn't block
        asyncio.create_task(scheduler_sync.run_now())
        logger.info("[STARTUP] BAS sync scheduler started")
    except Exception as e:
        logger.error(f"[STARTUP] Failed to start sync: {e}")


@app.on_event("shutdown")
async def shutdown():
    if not USE_MOCK and DATABASE_URL:
        try:
            from sync import scheduler_sync
            scheduler_sync.stop()
        except Exception:
            pass
