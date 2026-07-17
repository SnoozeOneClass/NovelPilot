from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.llm.gateway import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ResponseSchema,
    ToolChoice,
    ToolDefinition,
    call_llm,
)
from app.llm.redaction import redact_profile_secrets
from app.schemas.profiles import (
    LlmCapabilityCheck,
    LlmCapabilitySnapshot,
    LlmProfile,
    LlmProfilePublic,
    LlmProfileTestResult,
    LlmProfileUpsert,
    LlmProfilesPublicDocument,
)
from app.storage import profiles as profile_storage
from app.storage.projects import (
    ProjectReadOnlyError,
    ensure_creative_mutation_allowed,
    get_active_project_path,
    project_metadata_lock,
    read_project_metadata,
    write_project_metadata,
)

router = APIRouter()


@router.get("", response_model=LlmProfilesPublicDocument)
def list_profiles() -> LlmProfilesPublicDocument:
    return profile_storage.list_public_profiles()


@router.post("", response_model=LlmProfilePublic)
def upsert_profile(payload: LlmProfileUpsert) -> LlmProfilePublic:
    with _profile_mutation_transition() as project_path:
        try:
            profile = profile_storage.upsert_profile(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_active_project_profile(
            profile_storage.list_public_profiles().active_profile_id,
            project_path=project_path,
        )
        return profile


@router.put("/{profile_id}", response_model=LlmProfilePublic)
def update_profile(profile_id: str, payload: LlmProfileUpsert) -> LlmProfilePublic:
    if profile_id != payload.id:
        raise HTTPException(status_code=400, detail="Profile id cannot be changed.")
    with _profile_mutation_transition() as project_path:
        try:
            profile = profile_storage.upsert_profile(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _sync_active_project_profile(
            profile_storage.list_public_profiles().active_profile_id,
            project_path=project_path,
        )
        return profile


@router.post("/{profile_id}/select", response_model=LlmProfilesPublicDocument)
def select_profile(profile_id: str) -> LlmProfilesPublicDocument:
    with _profile_mutation_transition() as project_path:
        try:
            profiles = profile_storage.select_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}") from exc
        _sync_active_project_profile(profile_id, project_path=project_path)
        return profiles


@router.post("/{profile_id}/test", response_model=LlmProfileTestResult)
def test_profile(profile_id: str) -> LlmProfileTestResult:
    try:
        profile = profile_storage.get_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}") from exc
    if not profile.enabled:
        raise HTTPException(status_code=400, detail="Profile is disabled.")

    tool_check, tool_result = _test_tool_calling(profile)
    structured_check, structured_result = _test_structured_output(profile)
    ready = tool_check.ok and structured_check.ok
    capability_test = LlmCapabilitySnapshot(
        profile_fingerprint=profile_storage.profile_fingerprint(profile),
        tool_calling=tool_check,
        structured_output=structured_check,
        ready_for_harness=ready,
    )
    try:
        profile_storage.record_capability_test(profile.id, capability_test)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not ready:
        detail = (
            "Profile capability test failed. "
            f"Tool Calling: {tool_check.message} "
            f"Structured Output: {structured_check.message}"
        )
        raise HTTPException(
            status_code=502,
            detail=redact_profile_secrets(detail, profile),
        )

    result = structured_result or tool_result
    if result is None:
        raise HTTPException(status_code=502, detail="Profile capability test returned no result.")

    return LlmProfileTestResult(
        profile_id=profile.id,
        ok=True,
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        message="Tool Calling and Structured Output are available.",
        capability_test=capability_test,
    )


def _test_tool_calling(profile: LlmProfile) -> tuple[LlmCapabilityCheck, ChatResult | None]:
    try:
        result = call_llm(
            profile,
            ChatRequest(
                profile_id=profile.id,
                stream=False,
                messages=[
                    ChatMessage(
                        role="user",
                        content=(
                            "Call novelpilot_capability_echo exactly once with value set to ok."
                        ),
                    )
                ],
                tools=[
                    ToolDefinition(
                        name="novelpilot_capability_echo",
                        description="Non-mutating NovelPilot Tool Calling capability probe.",
                        input_schema={
                            "type": "object",
                            "properties": {"value": {"type": "string", "const": "ok"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    )
                ],
                tool_choice=ToolChoice(mode="named", name="novelpilot_capability_echo"),
            ),
        )
        if len(result.tool_calls) != 1:
            raise RuntimeError("provider did not return exactly one Tool call.")
        call = result.tool_calls[0]
        if call.parse_error is not None or call.arguments != {"value": "ok"}:
            raise RuntimeError("provider returned invalid Tool arguments.")
        return LlmCapabilityCheck(ok=True, message="supported"), result
    except (RuntimeError, ValueError) as exc:
        return LlmCapabilityCheck(
            ok=False,
            message=redact_profile_secrets(str(exc), profile)[:1_000],
        ), None


def _test_structured_output(
    profile: LlmProfile,
) -> tuple[LlmCapabilityCheck, ChatResult | None]:
    try:
        result = call_llm(
            profile,
            ChatRequest(
                profile_id=profile.id,
                stream=False,
                messages=[
                    ChatMessage(
                        role="user",
                        content="Return the capability result with supported set to true.",
                    )
                ],
                response_schema=ResponseSchema(
                    name="novelpilot_capability_result",
                    description="Non-mutating NovelPilot Structured Output capability probe.",
                    json_schema={
                        "type": "object",
                        "properties": {"supported": {"type": "boolean", "const": True}},
                        "required": ["supported"],
                        "additionalProperties": False,
                    },
                ),
            ),
        )
        if result.structured_output != {"supported": True}:
            raise RuntimeError("provider returned an invalid Structured Output result.")
        return LlmCapabilityCheck(ok=True, message="supported"), result
    except (RuntimeError, ValueError) as exc:
        return LlmCapabilityCheck(
            ok=False,
            message=redact_profile_secrets(str(exc), profile)[:1_000],
        ), None


@contextmanager
def _profile_mutation_transition() -> Iterator[Path | None]:
    with active_project_transition_lock():
        project_path = get_active_project_path()
        if project_path is not None and not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot change the active LLM profile while a harness runner is active."
                ),
            )
        try:
            if project_path is not None:
                try:
                    ensure_creative_mutation_allowed(project_path)
                except ProjectReadOnlyError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "实验母本源项目已经冻结，不能更改其活动模型配置。"
                        ),
                    ) from exc
            yield project_path
        finally:
            if project_path is not None:
                end_active_runner(project_path)


def _sync_active_project_profile(
    profile_id: str | None,
    *,
    project_path: Path | None = None,
) -> None:
    if profile_id is None:
        return
    project_path = project_path or get_active_project_path()
    if project_path is None:
        return
    with project_metadata_lock(project_path):
        metadata = read_project_metadata(project_path)
        if metadata.active_profile_id == profile_id:
            return
        metadata.active_profile_id = profile_id
        write_project_metadata(project_path, metadata)
