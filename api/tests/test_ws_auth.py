"""WebSocket auth rides in the first message, never in the URL (SPECS §5.3)."""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.enums import Role
from app.models.user import User
from app.security import create_access_token, decode_access_token


def test_query_string_token_is_ignored(_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app
    from app.ws import router

    decoded: list[str] = []

    def spy(token: str) -> dict:
        decoded.append(token)
        return decode_access_token(token)

    monkeypatch.setattr(router, "decode_access_token", spy)
    token = create_access_token(User(id=uuid.uuid4(), email="ws@dev.local", role=Role.admin))
    # A valid token in the URL authenticates nothing: the first message must be the auth one.
    with TestClient(app).websocket_connect(f"/api/v1/ws?token={token}") as ws:
        ws.send_json({"sub": f"run:{uuid.uuid4()}"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401
    assert decoded == []  # the URL token was never even looked at


def test_bad_first_message_token_is_refused(_database: str) -> None:
    from app.main import app

    with TestClient(app).websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "not-a-jwt"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401


def test_non_json_first_message_is_refused(_database: str) -> None:
    from app.main import app

    with TestClient(app).websocket_connect("/api/v1/ws") as ws:
        ws.send_text("hello")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401
