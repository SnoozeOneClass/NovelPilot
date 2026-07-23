"""Bounded Pydantic AI execution for frozen NovelPilot tasks.

The package intentionally has no eager re-exports: contracts are used by persistence,
while the registry imports domain DTOs. Loading both here would create an import cycle.
"""
