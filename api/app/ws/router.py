from __future__ import annotations

import asyncio
import json
import uuid

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.db import SessionLocal
from app.models.user import User
from app.security import decode_access_token
from app.ws.hub import RESYNC, NotifyHub, SignalQueue, channel_for

router = APIRouter()
_hub = NotifyHub()
_QUEUE_SIZE = 256


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    # Out-of-band auth (§5.3): tokens must not ride in the URL (they leak to proxy/LB logs,
    # history, Referer), so a query-string token is ignored. Accept first, then the client sends
    # `{"type": "auth", "token": ...}` as its first message; anything else closes with 4401.
    await websocket.accept()
    settings = get_settings()
    try:
        first = await asyncio.wait_for(
            websocket.receive_json(), settings.stackd_ws_auth_timeout_seconds
        )
    except WebSocketDisconnect:
        return
    except (ValueError, TimeoutError):  # not JSON, or no auth message in time
        await websocket.close(code=4401)
        return
    if not isinstance(first, dict) or first.get("type") != "auth":
        await websocket.close(code=4401)
        return
    token = first.get("token", "")
    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    async with SessionLocal() as session:
        user = await session.get(User, uuid.UUID(str(claims["sub"])))
    if user is None or user.disabled:
        await websocket.close(code=4401)
        return

    out: SignalQueue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    channels: dict[str, str] = {}  # NOTIFY channel → the subscription the client named it by
    subs: dict[str, str] = {}  # and back

    async def receive() -> None:
        while True:
            msg = await websocket.receive_json()
            if "unsub" in msg:
                channel = subs.pop(msg["unsub"], None)
                if channel is not None:
                    del channels[channel]
                    await _hub.unsubscribe(channel, out)
                continue
            sub = msg.get("sub", "")
            async with SessionLocal() as session:
                channel = await channel_for(sub, user, session)
            if channel is None or channel in channels:
                continue
            if len(channels) >= settings.stackd_ws_max_subscriptions:
                # The client reconnects and re-subscribes only to what it still wants.
                await websocket.close(code=1008)
                return
            channels[channel] = sub
            subs[sub] = channel
            await _hub.subscribe(channel, out)

    async def send() -> None:
        while (item := await out.get()) is not RESYNC:
            channel, payload = item
            sub = channels.get(channel)
            if sub is None:  # unsubscribed while the signal was queued
                continue
            # Tagged with its subscription: the client routes it to that channel's listeners only.
            await websocket.send_text(json.dumps({"sub": sub, **json.loads(payload)}))
        # Signals were lost while the hub was disconnected: make the client reconnect (§5.3).
        await websocket.close(code=1012)

    receiver = asyncio.create_task(receive())
    sender = asyncio.create_task(send())
    try:
        await asyncio.wait((receiver, sender), return_when=asyncio.FIRST_COMPLETED)
    finally:
        receiver.cancel()
        sender.cancel()
        await asyncio.gather(receiver, sender, return_exceptions=True)
        for channel in channels:
            await _hub.unsubscribe(channel, out)
    exc = None if receiver.cancelled() else receiver.exception()
    if exc is not None and not isinstance(exc, WebSocketDisconnect):
        raise exc
