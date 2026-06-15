from __future__ import annotations

import asyncio

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.security import decode_access_token
from app.ws.hub import NotifyHub, channel_for

router = APIRouter()
_hub = NotifyHub()


@router.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    # Access token in query string (browsers can't set WS headers). Validated before accept.
    token = websocket.query_params.get("token", "")
    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    user_id = str(claims["sub"])
    await websocket.accept()

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
            channel = channel_for(msg.get("sub", ""), user_id)
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
