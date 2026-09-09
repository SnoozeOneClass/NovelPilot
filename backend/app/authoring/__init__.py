"""Isolated fully automatic authoring runtime.

This package is an independent Python reimplementation informed by the public
ainovel-cli architecture.  It does not read or mutate NovelPilot's legacy
Book/Arc/Chapter runtime tables.
"""

from app.authoring.service import AuthoringService

__all__ = ["AuthoringService"]
