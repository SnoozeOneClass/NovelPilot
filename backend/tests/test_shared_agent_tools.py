from pathlib import Path

from app.harness.agents.models import AgentIdentity
from app.harness.agents.registry import ToolExecutionContext, ToolRegistry
from app.harness.agents.shared_tools import register_shared_tools
from app.llm.gateway import ToolCall
from app.schemas.projects import ProjectMetadata
from app.storage.json_files import read_json, write_json


def test_context_pack_is_role_authorized_and_excludes_uncommitted_prose(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    (tmp_path / "chapters" / "chapter-0001").mkdir(parents=True)
    (tmp_path / "chapters" / "chapter-0001" / "final.md").write_text(
        "committed prose", encoding="utf-8"
    )
    (tmp_path / "chapters" / "chapter-0001" / "draft.md").write_text(
        "secret candidate prose", encoding="utf-8"
    )
    registry = _registry()

    denied = registry.execute(
        _context(tmp_path, role="book", call_id="denied"),
        _call("denied", "get_loop_context", {"pack": "chapter_draft"}),
    )
    allowed = registry.execute(
        _context(tmp_path, role="chapter", call_id="allowed"),
        _call(
            "allowed",
            "get_loop_context",
            {"pack": "committed_chapters", "max_characters": 10_000},
        ),
    )

    assert denied.status == "error"
    assert denied.error_code == "context_pack_not_authorized"
    assert allowed.status == "ok"
    rendered = str(allowed.content)
    assert "committed prose" in rendered
    assert "secret candidate prose" not in rendered
    assert "chapters/*/draft.md" in rendered


def test_request_user_decision_requires_one_question_and_unique_suggestions(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    registry = _registry()
    invalid = registry.execute(
        _context(tmp_path, role="book", call_id="invalid"),
        _call(
            "invalid",
            "request_user_decision",
            {
                "question": "先确认地点？还是人物？",
                "suggestions": [
                    {"label": "A", "message": "地点"},
                    {"label": "B", "message": "人物"},
                ],
            },
        ),
    )
    valid = registry.execute(
        _context(tmp_path, role="book", call_id="valid"),
        _call(
            "valid",
            "request_user_decision",
            {
                "question": "六名核心人物应当分别承担什么叙事职责？",
                "context": "当前最影响后续规划的是人物范围。",
                "suggestions": [
                    {"label": "职责链", "message": "按责任链分配"},
                    {"label": "秘密链", "message": "按秘密链分配"},
                ],
            },
        ),
    )

    assert invalid.status == "error"
    assert invalid.error_code == "invalid_tool_arguments"
    assert valid.status == "ok"
    assert valid.terminal is True
    assert valid.checkpoint_id is not None
    wait_path = next(path for path in valid.artifact_paths if path.endswith("wait.json"))
    wait = read_json(tmp_path / wait_path)
    assert wait["question"] == "六名核心人物应当分别承担什么叙事职责？"
    assert len(wait["suggestions"]) == 2


def test_cross_loop_blocker_requires_complete_evidence_and_never_routes(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    registry = _registry()
    invalid = registry.execute(
        _context(tmp_path, role="chapter", call_id="invalid-blocker"),
        _call(
            "invalid-blocker",
            "report_blocker",
            {"kind": "cross_loop", "summary": "上层契约冲突"},
        ),
    )
    valid = registry.execute(
        _context(tmp_path, role="chapter", call_id="valid-blocker"),
        _call(
            "valid-blocker",
            "report_blocker",
            {
                "kind": "cross_loop",
                "summary": "章节目标与已提交事实无法同时满足",
                "evidence": ["chapters/chapter-0001/final.md#L12"],
                "target_owner": "story_arc",
                "contract_field": "required_outcome",
                "contract_revision": 2,
                "committed_evidence_locator": "chapters/chapter-0001/final.md#L12",
                "impossibility_reason": "既定死亡事实排除了当前目标。",
            },
        ),
    )

    assert invalid.status == "error"
    assert invalid.error_code == "invalid_tool_arguments"
    assert valid.status == "ok"
    assert valid.content["routing_status"] == "proposal_only"
    blocker_path = next(
        path for path in valid.artifact_paths if path.endswith("blocker.json")
    )
    blocker = read_json(tmp_path / blocker_path)
    assert blocker["routing_status"] == "proposal_only"
    assert blocker["target_owner"] == "story_arc"


def _project(project_path: Path) -> None:
    project_path.mkdir(parents=True, exist_ok=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(project_id="project-1").model_dump(mode="json"),
    )
    for relative in ["book", "arcs", "chapters", "canon"]:
        (project_path / relative).mkdir(exist_ok=True)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_shared_tools(registry)
    return registry


def _context(
    project_path: Path,
    *,
    role: str,
    call_id: str,
) -> ToolExecutionContext:
    scope_id = None if role == "book" else "scope-1"
    return ToolExecutionContext(
        project_path=project_path,
        identity=AgentIdentity(
            project_id="project-1",
            role=role,  # type: ignore[arg-type]
            scope_id=scope_id,
        ),
        candidate_run_id="run-1",
        activation_id="activation-1",
        tool_call_id=call_id,
        phase="test",
        expected_revision=1,
    )


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )
