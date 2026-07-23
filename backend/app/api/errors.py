from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from app.domain.arc.commands import ArcNotFoundError
from app.domain.book.commands import BookNotFoundError
from app.domain.chapter.commands import ChapterNotFoundError
from app.domain.commands import CommandPreconditionError, IdempotencyConflictError
from app.domain.projects import ProjectBusyError, ProjectNotFoundError
from app.profiles import ProfileConfigurationError

LOGGER = logging.getLogger(__name__)


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    details: dict[str, Any] | None = None


class ApiErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ApiError


def _response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ApiErrorEnvelope(
        error=ApiError(code=code, message=message, details=details)
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        issues = [
            {
                "type": issue["type"],
                "location": [str(value) for value in issue["loc"]],
                "message": issue["msg"],
            }
            for issue in exc.errors()
        ]
        return _response(
            status_code=422,
            code="request_validation_failed",
            message="The request does not satisfy the API contract.",
            details={"issues": issues},
        )

    @app.exception_handler(ProjectNotFoundError)
    @app.exception_handler(BookNotFoundError)
    @app.exception_handler(ArcNotFoundError)
    @app.exception_handler(ChapterNotFoundError)
    async def domain_not_found(request: Request, exc: Exception) -> JSONResponse:
        del request
        return _response(
            status_code=404,
            code="domain_object_not_found",
            message=str(exc) or "The requested domain object does not exist.",
        )

    @app.exception_handler(CommandPreconditionError)
    @app.exception_handler(ProjectBusyError)
    async def command_conflict(request: Request, exc: Exception) -> JSONResponse:
        del request
        return _response(
            status_code=409,
            code="command_precondition_failed",
            message=str(exc),
        )

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(request: Request, exc: Exception) -> JSONResponse:
        del request
        return _response(
            status_code=409,
            code="idempotency_conflict",
            message=str(exc),
        )

    @app.exception_handler(ProfileConfigurationError)
    async def profile_configuration_error(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del request
        return _response(
            status_code=422,
            code="profile_configuration_invalid",
            message=str(exc),
        )

    @app.exception_handler(IntegrityError)
    async def storage_conflict(request: Request, exc: IntegrityError) -> JSONResponse:
        del request, exc
        return _response(
            status_code=409,
            code="storage_constraint_conflict",
            message="The command conflicts with authoritative stored state.",
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        del request
        return _response(
            status_code=exc.status_code,
            code="http_error",
            message=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled API error on %s", request.url.path, exc_info=exc)
        return _response(
            status_code=500,
            code="internal_error",
            message="The backend encountered an unexpected error.",
        )
