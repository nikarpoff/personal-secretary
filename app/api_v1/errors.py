"""
Exception handlers mapping errors to HTTP responses.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.exceptions import AppError

INTERNAL_DETAIL = "Internal server error"


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    detail = str(exc) if exc.public else INTERNAL_DETAIL
    where = f"{request.method} {request.url.path}"
    logger.info(f"{where} -> {exc.status_code}: {exc.__class__.__name__}: {str(exc)[:200]}")

    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.opt(exception=exc).error(
        f"{request.method} {request.url.path} -> unhandled exception "
        f"{type(exc).__name__}: {exc}"
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": INTERNAL_DETAIL},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
