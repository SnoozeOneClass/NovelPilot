from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from app.agents.contracts import AgentRole
from app.agents.registry import TaskDefinition

ROLE_INSTRUCTIONS: dict[AgentRole, str] = {
    "book_strategist": (
        "You are NovelPilot's bounded Book Strategist. Work only on whole-book semantic design. "
        "You may propose content, but you cannot approve, commit, route, or mutate project state."
    ),
    "arc_planner": (
        "You are NovelPilot's bounded Story Arc Planner. Work only on the current Arc under frozen "
        "Book and Canon facts. You cannot approve, write chapters, or mutate project state."
    ),
    "chapter_writer": (
        "You are NovelPilot's bounded Chapter Writer. Work only on the requested chapter component "
        "under frozen upstream facts. You cannot commit Canon or alter an upstream contract."
    ),
    "evaluator": (
        "You are NovelPilot's independent read-only Evaluator. Apply only the supplied rubric and "
        "frozen evidence. Never rewrite candidate content, approve it, or mutate project state."
    ),
}


def build_agent(*, model: Model, definition: TaskDefinition) -> Agent[Any, Any]:
    """Create a fresh stateless Pydantic AI Agent for one frozen task activation."""
    instructions = f"{ROLE_INSTRUCTIONS[definition.role]}\n\n{definition.task_instructions}"
    if definition.output_mode == "native_json_schema":
        return Agent(
            model,
            instructions=instructions,
            output_type=NativeOutput(definition.output_model, strict=True),
            retries={"tools": 0, "output": 1},
        )
    return Agent(
        model,
        instructions=instructions,
        output_type=str,
        retries={"tools": 0, "output": 0},
    )
