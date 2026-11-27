"""Bounded request bodies for the endpoints that take raw bytes (saved plans, state documents)."""

from __future__ import annotations

from fastapi import Request

from app.errors import ProblemException


async def read_capped(request: Request, limit: int, title: str) -> bytes:
    """The request body, refused (413 `title`) past `limit` bytes without buffering more."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ProblemException(413, title, f"The limit is {limit} bytes.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ProblemException(413, title, f"The limit is {limit} bytes.")
    return bytes(body)
