from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.authoring.domain.models import ProjectView
from app.authoring.models.catalog import ProfileConfigurationError
from app.authoring.models.probe import ProfileCapabilityProbeError
from app.authoring.models.settings import (
    ModelSettingsConflictError,
    ModelSettingsDefaultInput,
    ModelSettingsDocument,
    ModelSettingsInputError,
    ModelSettingsProfileInput,
    ModelSettingsService,
)
from app.authoring.resources import AuthoringResources

router = APIRouter(prefix="/authoring", tags=["authoring"])


class ProfileBindingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_profile_id: str | None = None
    architect_profile_id: str | None = None
    writer_profile_id: str | None = None
    editor_profile_id: str | None = None
    arbiter_profile_id: str | None = None
    fallback_profile_id: str | None = None
    architect_fallback_profile_id: str | None = None
    writer_fallback_profile_id: str | None = None
    editor_fallback_profile_id: str | None = None
    arbiter_fallback_profile_id: str | None = None

    @field_validator(
        "default_profile_id",
        "architect_profile_id",
        "writer_profile_id",
        "editor_profile_id",
        "arbiter_profile_id",
        "fallback_profile_id",
        "architect_fallback_profile_id",
        "writer_fallback_profile_id",
        "editor_fallback_profile_id",
        "arbiter_fallback_profile_id",
        mode="before",
    )
    @classmethod
    def non_blank_profile_id(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Profile id must be non-empty")
        if len(normalized) > 200:
            raise ValueError("Profile id is too long")
        return normalized

    def to_store(self) -> dict[str, str]:
        values = {
            "default": self.default_profile_id,
            "architect": self.architect_profile_id,
            "writer": self.writer_profile_id,
            "editor": self.editor_profile_id,
            "arbiter": self.arbiter_profile_id,
            "fallback": self.fallback_profile_id,
            "fallback:architect": self.architect_fallback_profile_id,
            "fallback:writer": self.writer_fallback_profile_id,
            "fallback:editor": self.editor_fallback_profile_id,
            "fallback:arbiter": self.arbiter_fallback_profile_id,
        }
        return {key: value for key, value in values.items() if value is not None}


class CreateAuthoringProjectBody(ProfileBindingsBody):
    brief: str = Field(min_length=1, max_length=20_000)
    target_chapters: int | None = Field(default=None, ge=1, le=10_000)
    target_words: int | None = Field(default=None, ge=1, le=30_000_000)

    @field_validator("brief")
    @classmethod
    def non_blank_brief(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("brief must contain a story idea")
        return normalized

    @model_validator(mode="after")
    def one_optional_target(self) -> CreateAuthoringProjectBody:
        if self.target_chapters is not None and self.target_words is not None:
            raise ValueError("target_chapters and target_words are mutually exclusive")
        return self


class AuthoringActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    kind: str
    payload: dict[str, object]
    created_at: str


class AuthoringProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    brief: str
    title: str | None
    status: str
    stage: str
    target_chapters: int
    target_words: int | None
    completed_chapters: int
    progress_percent: int
    failure_reason: str | None
    latest_event_sequence: int
    profile_bindings: dict[str, str]
    recent_activity: list[AuthoringActivity]


class RunActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    ownership: Literal["started_here", "already_running_here", "owned_elsewhere"]
    project: AuthoringProjectResponse


class AuthoringEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    project_id: str
    kind: str
    payload: dict[str, object]
    created_at: str


class AuthoringProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    display_name: str
    model_id: str
    capability_status: Literal["missing", "stale", "ready"]
    context_window: int | None
    max_output_tokens: int | None


def _resources(request: Request) -> AuthoringResources:
    resources = getattr(request.app.state, "authoring_resources", None)
    if not isinstance(resources, AuthoringResources):
        raise HTTPException(status_code=503, detail="Authoring service is unavailable")
    return resources


@contextmanager
def _model_settings(request: Request) -> Iterator[ModelSettingsService]:
    resources = _resources(request)
    try:
        yield ModelSettingsService(resources.profile_catalog, resources.metadata_path)
    except ModelSettingsConflictError:
        raise HTTPException(
            status_code=409, detail="模型设置已在验证期间更改，请重新验证。"
        ) from None
    except ProfileCapabilityProbeError:
        raise HTTPException(
            status_code=422, detail="模型连接或能力验证失败，请检查连接、密钥和模型设置。"
        ) from None
    except ModelSettingsInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except (ProfileConfigurationError, ValueError):
        raise HTTPException(
            status_code=422, detail="模型配置或元数据无效，请检查设置后重试。"
        ) from None
    except OSError:
        raise HTTPException(
            status_code=503, detail="模型设置暂时无法保存或读取，请稍后重试。"
        ) from None


@router.get("/model-settings", response_model=ModelSettingsDocument)
def get_model_settings(request: Request) -> ModelSettingsDocument:
    with _model_settings(request) as settings:
        return settings.get()


@router.put("/model-settings/profiles/{profile_id:path}", response_model=ModelSettingsDocument)
def save_model_settings_profile(
    profile_id: str, body: ModelSettingsProfileInput, request: Request
) -> ModelSettingsDocument:
    with _model_settings(request) as settings:
        return settings.save(profile_id, body)


@router.post(
    "/model-settings/profiles/{profile_id:path}/validate", response_model=ModelSettingsDocument
)
async def validate_model_settings_profile(
    profile_id: str, request: Request, body: Annotated[None, Body()] = None
) -> ModelSettingsDocument:
    with _model_settings(request) as settings:
        return await settings.validate(profile_id)


@router.put("/model-settings/default", response_model=ModelSettingsDocument)
def select_default_model(
    body: ModelSettingsDefaultInput, request: Request
) -> ModelSettingsDocument:
    with _model_settings(request) as settings:
        return settings.select_default(body.profile_id)


async def _project_response(
    resources: AuthoringResources, project_id: str
) -> AuthoringProjectResponse:
    try:
        # Read the event cursor first. If a commit lands between these reads,
        # the projection may be ahead of the cursor and SSE will replay that
        # event. Reading in the opposite order can advance the cursor past a
        # fact that was absent from the returned projection.
        events = await resources.service.events(project_id)
        project = await resources.service.status(project_id)
        bindings = await resources.store.profile_bindings(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    return _project_projection(project, bindings, events)


def _project_projection(
    project: ProjectView,
    bindings: dict[str, str],
    events: list[dict[str, object]],
) -> AuthoringProjectResponse:
    latest = _event_sequence(events[-1]) if events else 0
    progress = min(100, round(project.chapter_count / project.target.target_chapters * 100))
    stage = {
        "foundation": "preparing",
        "writing": "writing",
        "finalizing": "finishing",
        "complete": "complete",
    }[project.phase.value]
    return AuthoringProjectResponse(
        project_id=project.id,
        brief=project.brief,
        title=project.title,
        status=project.status.value,
        stage=stage,
        target_chapters=project.target.target_chapters,
        target_words=project.target.target_words,
        completed_chapters=project.chapter_count,
        progress_percent=progress,
        failure_reason=_public_failure_reason(project),
        latest_event_sequence=latest,
        profile_bindings=bindings,
        recent_activity=[
            AuthoringActivity(
                sequence=_event_sequence(event),
                kind=str(event["kind"]),
                payload=_event_payload(event),
                created_at=str(event["created_at"]),
            )
            for event in events[-20:]
        ],
    )


def _public_failure_reason(project: ProjectView) -> str | None:
    if project.failure_reason is None:
        return None
    normalized = project.failure_reason.casefold()
    if normalized.startswith(
        (
            "activationrequestbudgetexhausted:",
            "modelrequestbudgetexhausted:",
            "transportretrybudgetexhausted:",
        )
    ):
        return "本次创作步骤达到运行上限，系统已安全暂停。"
    if "profile" in normalized or "model" in normalized:
        return "模型设置当前不可用，请检查设置后继续。"
    if "context" in normalized or "compaction" in normalized:
        return "本次内容超过了模型可处理的范围，系统已安全暂停。"
    return "创作暂时无法继续，系统已安全暂停。"


@router.post("/projects", response_model=AuthoringProjectResponse, status_code=201)
async def create_project(
    body: CreateAuthoringProjectBody, request: Request
) -> AuthoringProjectResponse:
    resources = _resources(request)
    try:
        resources.validate_profile_bindings(body.to_store())
    except ProfileConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    project_id = await resources.service.create(
        brief=body.brief.strip(),
        target_chapters=body.target_chapters,
        target_words=body.target_words,
        profile_bindings=body.to_store(),
    )
    return await _project_response(resources, project_id)


@router.get("/projects", response_model=list[AuthoringProjectResponse])
async def list_projects(request: Request) -> list[AuthoringProjectResponse]:
    resources = _resources(request)
    projects = await resources.service.projects()
    return [await _project_response(resources, project.id) for project in projects]


@router.get("/projects/{project_id}", response_model=AuthoringProjectResponse)
async def get_project(project_id: str, request: Request) -> AuthoringProjectResponse:
    return await _project_response(_resources(request), project_id)


@router.post("/projects/{project_id}/run", response_model=RunActionResponse)
async def start_or_resume(project_id: str, request: Request) -> RunActionResponse:
    resources = _resources(request)
    try:
        ownership = await resources.runs.start_or_resume(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await asyncio.sleep(0)
    return RunActionResponse(
        accepted=ownership != "owned_elsewhere",
        ownership=ownership,
        project=await _project_response(resources, project_id),
    )


@router.post("/projects/{project_id}/pause", response_model=RunActionResponse)
async def pause_project(project_id: str, request: Request) -> RunActionResponse:
    resources = _resources(request)
    try:
        owned_here = await resources.runs.is_active(project_id)
        await resources.runs.pause(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RunActionResponse(
        accepted=True,
        ownership="already_running_here" if owned_here else "owned_elsewhere",
        project=await _project_response(resources, project_id),
    )


@router.post("/projects/{project_id}/cancel", response_model=RunActionResponse)
async def cancel_project(project_id: str, request: Request) -> RunActionResponse:
    resources = _resources(request)
    try:
        owned_here = await resources.runs.is_active(project_id)
        await resources.runs.cancel(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RunActionResponse(
        accepted=True,
        ownership="already_running_here" if owned_here else "owned_elsewhere",
        project=await _project_response(resources, project_id),
    )


@router.put("/projects/{project_id}/profiles", response_model=AuthoringProjectResponse)
async def update_profiles(
    project_id: str, body: ProfileBindingsBody, request: Request
) -> AuthoringProjectResponse:
    resources = _resources(request)
    try:
        await resources.service.status(project_id)
        resources.validate_profile_bindings(body.to_store())
        await resources.store.update_profile_bindings(project_id, body.to_store())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    except ProfileConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return await _project_response(resources, project_id)


@router.get("/profiles", response_model=list[AuthoringProfileView])
def list_profiles(request: Request) -> list[AuthoringProfileView]:
    with _model_settings(request) as settings:
        document = settings.get(current_metadata_only=True)
        return [
            AuthoringProfileView(
                profile_id=profile.profile_id,
                display_name=profile.display_name,
                model_id=profile.model_id,
                capability_status=profile.capability_status,
                context_window=profile.context_window,
                max_output_tokens=profile.max_output_tokens,
            )
            for profile in document.profiles
        ]


@router.get("/projects/{project_id}/export")
async def export_project(
    project_id: str,
    request: Request,
    format: Literal["markdown", "txt"] = Query(default="markdown"),
) -> Response:
    resources = _resources(request)
    try:
        manuscript, digest = await resources.service.export(project_id, format=format)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Authoring project not found") from error
    extension = "md" if format == "markdown" else "txt"
    media_type = "text/markdown" if format == "markdown" else "text/plain"
    return Response(
        content=manuscript.encode("utf-8"),
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="novel.{extension}"',
            "ETag": f'"{digest}"',
        },
    )


LastEventId = Annotated[str | None, Header(alias="Last-Event-ID")]


@router.get("/projects/{project_id}/events")
async def stream_events(
    project_id: str,
    request: Request,
    last_event_id: LastEventId = None,
    after: int = Query(default=0, ge=0),
    follow: bool = Query(default=True),
) -> StreamingResponse:
    resources = _resources(request)
    await _project_response(resources, project_id)
    cursor = after
    if last_event_id is not None:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID must be an integer"
            ) from error

    async def generate() -> AsyncIterator[str]:
        current = cursor
        while True:
            events = await resources.service.events(project_id, after_seq=current)
            for event in events:
                sequence = _event_sequence(event)
                value = AuthoringEventView(
                    sequence=sequence,
                    project_id=project_id,
                    kind=str(event["kind"]),
                    payload=_event_payload(event),
                    created_at=str(event["created_at"]),
                )
                current = sequence
                yield (
                    f"id: {sequence}\nevent: authoring_event\ndata: {value.model_dump_json()}\n\n"
                )
            if not follow or await request.is_disconnected():
                return
            if not events:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_sequence(event: dict[str, object]) -> int:
    value = event.get("seq")
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("authoring event sequence is invalid")
    return value


def _event_payload(event: dict[str, object]) -> dict[str, object]:
    value = event.get("payload")
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeError("authoring event payload is invalid")
    return value
