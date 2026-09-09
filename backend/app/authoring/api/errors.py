from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.authoring.models.settings import ModelSettingsProfileInput

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
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ApiErrorEnvelope(error=ApiError(code=code, message=message, details=details))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError) -> JSONResponse:
        settings_request = request.url.path.startswith("/api/authoring/model-settings")
        issues = [
            {
                "type": item["type"],
                "location": (
                    [
                        str(value)
                        for value in item["loc"][:2]
                        if value
                        in {"body", "path", "profile_id", *ModelSettingsProfileInput.model_fields}
                    ]
                    if settings_request
                    else [str(value) for value in item["loc"]]
                ),
                "message": (
                    "Invalid JSON request body."
                    if settings_request and item["type"] == "json_invalid"
                    else item["msg"]
                ),
            }
            for item in error.errors()
        ]
        return _response(
            422,
            "request_validation_failed",
            "The request does not satisfy the API contract.",
            {"issues": issues},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        return _response(error.status_code, "http_error", str(error.detail))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        if request.url.path.startswith("/api/authoring/model-settings"):
            # Provider causes and invalid secret-bearing input can live in a
            # chained exception. Log only its class at this write-only boundary.
            LOGGER.error("Unhandled model settings API error (%s)", type(error).__name__)
        else:
            LOGGER.exception(
                "Unhandled Authoring API error at %s", request.url.path, exc_info=error
            )
        return _response(500, "internal_error", "The backend encountered an unexpected error.")
