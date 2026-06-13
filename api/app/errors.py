from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ProblemException(Exception):
    """RFC 9457 problem+json error (SPECS §1)."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        type_: str = "about:blank",
        **extra: object,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.extra = extra
        super().__init__(detail or title)


def _problem(
    status: int, title: str, detail: str | None, type_: str, **extra: object
) -> JSONResponse:
    body: dict[str, object] = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_CONTENT_TYPE)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemException)
    async def _handle_problem(_: Request, exc: ProblemException) -> JSONResponse:
        return _problem(exc.status, exc.title, exc.detail, exc.type, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(exc.status_code, str(exc.detail), None, "about:blank")

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic v2 puts the raw exception in `ctx` for custom-validator errors; coerce it to a
        # string so the problem+json body stays serializable.
        errors = []
        for e in exc.errors():
            ctx = e.get("ctx")
            if ctx:
                e = {
                    **e,
                    "ctx": {k: (str(v) if isinstance(v, Exception) else v) for k, v in ctx.items()},
                }
            errors.append(e)
        return _problem(
            422,
            "Validation error",
            "Request payload failed validation.",
            "about:blank",
            errors=errors,
        )
