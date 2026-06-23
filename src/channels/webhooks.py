"""
Inbound webhook endpoints for push channels (WAHA / Viber). Mounted on the main
FastAPI app. Each route resolves the adapter from the registry by account_id,
verifies the per-account webhook secret, and hands the payload to the adapter.
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from src.channels import registry

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify(channel: str, account_id: int, request: Request):
    adapter = registry.get(channel, account_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail="account not connected")
    secret = (adapter.credentials or {}).get("webhook_secret") or getattr(adapter, "webhook_secret", "")
    if secret:
        provided = request.query_params.get("token") or request.headers.get("X-Webhook-Secret")
        if provided != secret:
            raise HTTPException(status_code=403, detail="bad webhook secret")
    return adapter


@router.post("/webhooks/waha/{account_id}")
async def waha_webhook(account_id: int, request: Request):
    adapter = _verify("whatsapp", account_id, request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        await adapter.handle_webhook(payload)
    except Exception as e:
        logger.error(f"[WEBHOOK] waha/{account_id} error: {e}")
    return {"ok": True}


@router.post("/webhooks/viber/{account_id}")
async def viber_webhook(account_id: int, request: Request):
    # Viber sends a verification POST on set_webhook; we must answer 200 fast.
    adapter = _verify("viber", account_id, request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        await adapter.handle_webhook(payload)
    except Exception as e:
        logger.error(f"[WEBHOOK] viber/{account_id} error: {e}")
    return {"ok": True}
