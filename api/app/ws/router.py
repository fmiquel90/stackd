from __future__ import annotations

import asyncio
import uuid

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db import SessionLocal
from app.models.user import User
from app.security import decode_access_token
from app.ws.hub import NotifyHub, channel_for

router = APIRouter()
_hub = NotifyHub()


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    # Out-of-band auth: tokens must not ride in the URL (they leak to proxy/LB logs, history,
    # Referer). Accept first, then the client sends `{"type": "auth", "token": ...}` as its first
    # message. A query-string token is still honored as a fallback for older clients.
    await websocket.accept()
    token = websocket.query_params.get("token", "")
    if not token:
        try:
            first = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        if first.get("type") != "auth":
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

    out: asyncio.Queue[str] = asyncio.Queue()
    subscriptions: list[tuple[str, asyncio.Queue, asyncio.Task]] = []

    async def forward(channel: str, queue: asyncio.Queue) -> None:
        while True:
            out.put_nowait(await queue.get())

    async def sender() -> None:
        while True:
            await websocket.send_text(await out.get())

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await websocket.receive_json()
            async with SessionLocal() as session:
                channel = await channel_for(msg.get("sub", ""), user, session)
            if channel:
                queue = await _hub.subscribe(channel)
                subscriptions.append((channel, queue, asyncio.create_task(forward(channel, queue))))
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        for channel, queue, task in subscriptions:
            task.cancel()
            await _hub.unsubscribe(channel, queue)
