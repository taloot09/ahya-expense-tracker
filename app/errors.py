from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: list | None = None, headers: dict | None = None):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details or [], headers


def error_body(code: str, message: str, details: list | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or []}}


def not_found(what: str = "resource") -> AppError:
    return AppError(404, "not_found", f"{what} not found")


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 413: "payload_too_large"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(error_body(exc.code, exc.message, exc.details), exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]}
                   for e in exc.errors()]
        return JSONResponse(error_body("validation_error", "Invalid input", details), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, "error")
        return JSONResponse(error_body(code, str(exc.detail)), exc.status_code, headers=exc.headers)
