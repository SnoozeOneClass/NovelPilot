from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from app.schemas.experiments import ExperimentHookStrategy


HookPoint = Literal["tool_result"]
HookObserver = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ExperimentHookSpec:
    hook_id: str
    point: HookPoint
    observer: HookObserver


class ExperimentHookRegistry:
    """Experiment-only observation hooks, separate from model-facing Tools.

    Observers receive a deep copy after the ordinary Tool authorization, validation,
    transaction, and audit steps. Their return value is ignored, so a disabled or
    malicious experiment observer cannot add Tool authority or change commit gates.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, ExperimentHookSpec] = {}

    def register(self, spec: ExperimentHookSpec) -> None:
        if not spec.hook_id or spec.hook_id in self._hooks:
            raise ValueError(f"Experiment hook is already registered: {spec.hook_id}")
        self._hooks[spec.hook_id] = spec

    def registered_ids(self) -> list[str]:
        return sorted(self._hooks)

    def observe(
        self,
        point: HookPoint,
        strategy: ExperimentHookStrategy,
        payload: dict[str, Any],
    ) -> list[str]:
        if strategy.mode == "none":
            raise ValueError("The frozen none/direct-v1 baseline cannot execute Harness hooks.")
        observed: list[str] = []
        disabled = set(strategy.disabled_hook_ids)
        unknown = sorted(disabled - self._hooks.keys())
        if unknown:
            raise ValueError("Experiment strategy disables unknown hooks: " + ", ".join(unknown))
        for hook_id in self.registered_ids():
            spec = self._hooks[hook_id]
            if spec.point != point or hook_id in disabled:
                continue
            spec.observer(deepcopy(payload))
            observed.append(hook_id)
        return observed
