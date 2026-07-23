from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import (
    BLOB,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql.schema import Constraint

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

WORKSPACE_STATES = ("idle", "active", "blocked_by_user", "blocked_by_upstream", "stale")
SUBMISSION_DISPOSITIONS = ("pending", "superseded", "rejected", "promoted")
CHANGE_REQUEST_STATUSES = ("open", "resolved", "rejected", "superseded")


def _ck(name: str, expression: str) -> CheckConstraint:
    return CheckConstraint(expression, name=name)


def _enum_ck(name: str, column: str, values: Sequence[str]) -> CheckConstraint:
    quoted_values = ", ".join(f"'{value}'" for value in values)
    return _ck(name, f"{column} IN ({quoted_values})")


def _sha_expression(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _non_blank_expression(column: str) -> str:
    return f"length(trim({column})) > 0"


def _project_owner_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["project_id"],
        ["projects.id"],
        ondelete="CASCADE",
        onupdate="RESTRICT",
    )


def _content_ref_fk(column_name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["project_id", column_name],
        ["content_refs.project_id", "content_refs.id"],
        onupdate="RESTRICT",
    )


def _content_ref_fks(*column_names: str) -> list[ForeignKeyConstraint]:
    return [_content_ref_fk(column_name) for column_name in column_names]


def _project_owned_constraints() -> list[Constraint]:
    return [_project_owner_fk(), UniqueConstraint("project_id", "id")]


def _submission_close_ck(prefix: str = "") -> CheckConstraint:
    return _ck(
        f"{prefix}disposition_close_fields",
        "((disposition = 'pending' AND close_reason_code IS NULL AND closed_at_ms IS NULL) "
        "OR (disposition <> 'pending' AND close_reason_code IS NOT NULL "
        "AND closed_at_ms IS NOT NULL))",
    )


def _workspace_common_checks() -> list[CheckConstraint]:
    return [
        _enum_ck("workspace_state", "state", WORKSPACE_STATES),
        _ck("workspace_lock_version_positive", "lock_version >= 1"),
        _ck(
            "workspace_repair_policy",
            "repair_policy_id = 'semantic-repair-v1' AND semantic_repair_limit = 5",
        ),
        _ck(
            "workspace_repair_count_range",
            "semantic_repair_count >= 0 AND semantic_repair_count <= semantic_repair_limit",
        ),
        _ck(
            "workspace_stale_fields",
            "((state = 'stale' AND stale_reason_code IS NOT NULL AND stale_at_ms IS NOT NULL) "
            "OR (state <> 'stale' AND stale_reason_code IS NULL AND stale_at_ms IS NULL))",
        ),
    ]


# Identity/current -----------------------------------------------------------------

projects = Table(
    "projects",
    metadata,
    Column("id", String, primary_key=True),
    Column("operation_mode", String, nullable=False),
    Column("lifecycle_status", String, nullable=False),
    Column("settings_lock_version", Integer, nullable=False, server_default=text("1")),
    Column("default_profile_id", String),
    Column("book_profile_id", String),
    Column("arc_profile_id", String),
    Column("chapter_profile_id", String),
    Column("evaluator_profile_id", String),
    Column("current_canon_baseline_id", String, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    _enum_ck("operation_mode", "operation_mode", ("full_auto", "participatory")),
    _enum_ck("lifecycle_status", "lifecycle_status", ("active", "completed")),
    _ck("settings_lock_version_positive", "settings_lock_version >= 1"),
    _ck(
        "profile_ids_non_blank",
        "(default_profile_id IS NULL OR length(trim(default_profile_id)) > 0) "
        "AND (book_profile_id IS NULL OR length(trim(book_profile_id)) > 0) "
        "AND (arc_profile_id IS NULL OR length(trim(arc_profile_id)) > 0) "
        "AND (chapter_profile_id IS NULL OR length(trim(chapter_profile_id)) > 0) "
        "AND (evaluator_profile_id IS NULL OR length(trim(evaluator_profile_id)) > 0)",
    ),
    ForeignKeyConstraint(
        ["id", "current_canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
)

books = Table(
    "books",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("lifecycle_status", String, nullable=False),
    Column("current_baseline_id", String),
    Column("current_completion_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("project_id"),
    _enum_ck("lifecycle_status", "lifecycle_status", ("developing", "active", "completed")),
    _ck(
        "lifecycle_pointers",
        "((lifecycle_status = 'developing' AND current_baseline_id IS NULL "
        "AND current_completion_id IS NULL) "
        "OR (lifecycle_status = 'active' AND current_baseline_id IS NOT NULL "
        "AND current_completion_id IS NULL) "
        "OR (lifecycle_status = 'completed' AND current_baseline_id IS NOT NULL "
        "AND current_completion_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "id", "current_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["project_id", "id", "current_completion_id"],
        ["book_completions.project_id", "book_completions.book_id", "book_completions.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
)

story_arcs = Table(
    "story_arcs",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("purpose", String, nullable=False),
    Column("lifecycle_status", String, nullable=False),
    Column("current_baseline_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("completed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "book_id", "id"),
    UniqueConstraint("project_id", "book_id", "id", "purpose"),
    UniqueConstraint("book_id", "ordinal"),
    _ck("ordinal_positive", "ordinal >= 1"),
    _enum_ck("purpose", "purpose", ("regular", "final")),
    _enum_ck("lifecycle_status", "lifecycle_status", ("planning", "active", "completed")),
    _ck(
        "active_baseline_required",
        "lifecycle_status = 'planning' OR current_baseline_id IS NOT NULL",
    ),
    _ck(
        "completion_timestamp",
        "((lifecycle_status = 'completed' AND completed_at_ms IS NOT NULL) "
        "OR (lifecycle_status <> 'completed' AND completed_at_ms IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id"],
        ["books.project_id", "books.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "id", "current_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
)
Index(
    "uq_arc_one_unfinished_per_book",
    story_arcs.c.book_id,
    unique=True,
    sqlite_where=story_arcs.c.lifecycle_status.in_(("planning", "active")),
)

chapters = Table(
    "chapters",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("book_ordinal", Integer, nullable=False),
    Column("arc_ordinal", Integer, nullable=False),
    Column("lifecycle_status", String, nullable=False),
    Column("current_baseline_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("committed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "book_id", "arc_id", "id"),
    UniqueConstraint("book_id", "book_ordinal"),
    UniqueConstraint("arc_id", "arc_ordinal"),
    _ck("ordinals_positive", "book_ordinal >= 1 AND arc_ordinal >= 1"),
    _enum_ck("lifecycle_status", "lifecycle_status", ("drafting", "committed")),
    _ck(
        "commit_fields",
        "((lifecycle_status = 'drafting' AND committed_at_ms IS NULL) "
        "OR (lifecycle_status = 'committed' AND current_baseline_id IS NOT NULL "
        "AND committed_at_ms IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id"],
        ["story_arcs.project_id", "story_arcs.book_id", "story_arcs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "id", "current_baseline_id"],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
)
Index(
    "uq_chapter_one_drafting_per_book",
    chapters.c.book_id,
    unique=True,
    sqlite_where=chapters.c.lifecycle_status == "drafting",
)
Index(
    "uq_chapter_one_drafting_per_arc",
    chapters.c.arc_id,
    unique=True,
    sqlite_where=chapters.c.lifecycle_status == "drafting",
)


# Project-owned content ------------------------------------------------------------

content_blobs = Table(
    "content_blobs",
    metadata,
    Column("project_id", String, nullable=False),
    Column("sha256", String, nullable=False),
    Column("compression", String, nullable=False),
    Column("canonical_size", Integer, nullable=False),
    Column("stored_size", Integer, nullable=False),
    Column("payload", BLOB, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    _project_owner_fk(),
    _enum_ck("compression", "compression", ("identity-v1", "gzip-v1")),
    _ck("sha256", _sha_expression("sha256")),
    _ck("sizes_non_negative", "canonical_size >= 0 AND stored_size >= 0"),
    _ck("stored_size_matches_payload", "length(payload) = stored_size"),
    PrimaryKeyConstraint("project_id", "sha256"),
)

content_refs = Table(
    "content_refs",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("blob_sha256", String, nullable=False),
    Column("semantic_kind", String, nullable=False),
    Column("media_type", String, nullable=False),
    Column("canonicalizer_id", String, nullable=False),
    Column("schema_id", String),
    Column("schema_version", Integer),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    _ck(
        "descriptors_non_blank",
        f"{_non_blank_expression('semantic_kind')} "
        f"AND {_non_blank_expression('media_type')} "
        f"AND {_non_blank_expression('canonicalizer_id')}",
    ),
    _ck(
        "schema_pair",
        "((schema_id IS NULL AND schema_version IS NULL) "
        "OR (schema_id IS NOT NULL AND length(trim(schema_id)) > 0 "
        "AND schema_version >= 1))",
    ),
    ForeignKeyConstraint(
        ["project_id", "blob_sha256"],
        ["content_blobs.project_id", "content_blobs.sha256"],
    ),
)


# Book lifecycle -------------------------------------------------------------------

book_workspaces = Table(
    "book_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("lock_version", Integer, nullable=False),
    Column("base_book_baseline_id", String),
    Column("base_canon_baseline_id", String, nullable=False),
    Column("direction_draft_ref_id", String, nullable=False),
    Column("discussion_state_ref_id", String, nullable=False),
    Column("transcript_ref_id", String, nullable=False),
    Column("candidate_constraints_ref_id", String),
    Column("candidate_titles_ref_id", String),
    Column("candidate_rolling_plan_ref_id", String),
    Column("candidate_completion_contract_ref_id", String),
    Column("guidance_ref_id", String),
    Column("readiness_status", String, nullable=False),
    Column("repair_policy_id", String, nullable=False),
    Column("semantic_repair_count", Integer, nullable=False),
    Column("semantic_repair_limit", Integer, nullable=False),
    Column("stale_reason_code", String),
    Column("stale_at_ms", Integer),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("book_id"),
    UniqueConstraint("project_id", "book_id", "id"),
    *_workspace_common_checks(),
    _enum_ck("readiness_status", "readiness_status", ("continue", "ready")),
    _ck(
        "candidate_refs_all_or_none",
        "((candidate_constraints_ref_id IS NULL AND candidate_titles_ref_id IS NULL "
        "AND candidate_rolling_plan_ref_id IS NULL "
        "AND candidate_completion_contract_ref_id IS NULL) "
        "OR (candidate_constraints_ref_id IS NOT NULL AND candidate_titles_ref_id IS NOT NULL "
        "AND candidate_rolling_plan_ref_id IS NOT NULL "
        "AND candidate_completion_contract_ref_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id"],
        ["books.project_id", "books.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "base_book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "base_canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    *_content_ref_fks(
        "direction_draft_ref_id",
        "discussion_state_ref_id",
        "transcript_ref_id",
        "candidate_constraints_ref_id",
        "candidate_titles_ref_id",
        "candidate_rolling_plan_ref_id",
        "candidate_completion_contract_ref_id",
        "guidance_ref_id",
    ),
)

book_review_submissions = Table(
    "book_review_submissions",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("workspace_id", String, nullable=False),
    Column("workspace_lock_version", Integer, nullable=False),
    Column("base_book_baseline_id", String),
    Column("canon_baseline_id", String, nullable=False),
    Column("direction_ref_id", String, nullable=False),
    Column("constraints_ref_id", String, nullable=False),
    Column("titles_ref_id", String, nullable=False),
    Column("rolling_plan_ref_id", String, nullable=False),
    Column("completion_contract_ref_id", String, nullable=False),
    Column("content_manifest_ref_id", String, nullable=False),
    Column("content_fingerprint", String, nullable=False),
    Column("disposition", String, nullable=False),
    Column("close_reason_code", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("closed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "book_id", "id"),
    _ck("workspace_lock_version_positive", "workspace_lock_version >= 1"),
    _ck("content_fingerprint", _sha_expression("content_fingerprint")),
    _enum_ck("disposition", "disposition", SUBMISSION_DISPOSITIONS),
    _submission_close_ck(),
    ForeignKeyConstraint(
        ["project_id", "book_id", "workspace_id"],
        ["book_workspaces.project_id", "book_workspaces.book_id", "book_workspaces.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "base_book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    *_content_ref_fks(
        "direction_ref_id",
        "constraints_ref_id",
        "titles_ref_id",
        "rolling_plan_ref_id",
        "completion_contract_ref_id",
        "content_manifest_ref_id",
    ),
)
Index(
    "uq_submission_one_pending_per_book",
    book_review_submissions.c.book_id,
    unique=True,
    sqlite_where=book_review_submissions.c.disposition == "pending",
)

book_reviews = Table(
    "book_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("evaluator_task_id", String, nullable=False),
    Column("evaluator_attempt_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("rubric_id", String, nullable=False),
    Column("rubric_version", Integer, nullable=False),
    Column("precheck_ref_id", String, nullable=False),
    Column("detail_ref_id", String, nullable=False),
    Column("repair_contract_ref_id", String),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("submission_id"),
    UniqueConstraint("project_id", "book_id", "submission_id", "id"),
    _enum_ck("decision", "decision", ("pass", "local_repair", "needs_user")),
    _ck("rubric_version_positive", "rubric_version >= 1"),
    _ck(
        "repair_contract",
        "((decision = 'local_repair' AND repair_contract_ref_id IS NOT NULL) "
        "OR (decision <> 'local_repair' AND repair_contract_ref_id IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "submission_id"],
        [
            "book_review_submissions.project_id",
            "book_review_submissions.book_id",
            "book_review_submissions.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "evaluator_task_id", "evaluator_attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
    ),
    *_content_ref_fks("precheck_ref_id", "detail_ref_id", "repair_contract_ref_id"),
)

book_approvals = Table(
    "book_approvals",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("selected_title", String),
    Column("title_source", String),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint("project_id", "book_id", "submission_id", "review_id", "id"),
    _enum_ck("decision", "decision", ("approved", "rejected")),
    _ck(
        "approval_title",
        "((decision = 'approved' AND selected_title IS NOT NULL "
        "AND length(trim(selected_title)) > 0 "
        "AND title_source IN ('recommended', 'custom')) "
        "OR (decision = 'rejected' AND selected_title IS NULL AND title_source IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "submission_id", "review_id"],
        [
            "book_reviews.project_id",
            "book_reviews.book_id",
            "book_reviews.submission_id",
            "book_reviews.id",
        ],
    ),
)

book_baselines = Table(
    "book_baselines",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("baseline_version", Integer, nullable=False),
    Column("parent_baseline_id", String),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("approval_id", String, nullable=False),
    Column("approved_title", String, nullable=False),
    Column("title_source", String, nullable=False),
    Column("direction_ref_id", String, nullable=False),
    Column("constraints_ref_id", String, nullable=False),
    Column("rolling_plan_ref_id", String, nullable=False),
    Column("completion_contract_ref_id", String, nullable=False),
    Column("minimum_chapter_count", Integer, nullable=False),
    Column("maximum_chapter_count", Integer, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("book_id", "baseline_version"),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint("approval_id"),
    UniqueConstraint("project_id", "book_id", "id"),
    _ck(
        "version_parent",
        "((baseline_version = 1 AND parent_baseline_id IS NULL) "
        "OR (baseline_version >= 2 AND parent_baseline_id IS NOT NULL))",
    ),
    _ck(
        "chapter_count_range",
        "minimum_chapter_count >= 1 AND maximum_chapter_count >= minimum_chapter_count",
    ),
    _ck("approved_title_non_blank", _non_blank_expression("approved_title")),
    _enum_ck("title_source", "title_source", ("recommended", "custom")),
    ForeignKeyConstraint(
        ["project_id", "book_id"],
        ["books.project_id", "books.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "parent_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "submission_id", "review_id", "approval_id"],
        [
            "book_approvals.project_id",
            "book_approvals.book_id",
            "book_approvals.submission_id",
            "book_approvals.review_id",
            "book_approvals.id",
        ],
    ),
    *_content_ref_fks(
        "direction_ref_id",
        "constraints_ref_id",
        "rolling_plan_ref_id",
        "completion_contract_ref_id",
    ),
)

book_completions = Table(
    "book_completions",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("completion_version", Integer, nullable=False),
    Column("parent_completion_id", String),
    Column("book_baseline_id", String, nullable=False),
    Column("terminal_arc_id", String, nullable=False),
    Column("terminal_arc_baseline_id", String, nullable=False),
    Column("terminal_chapter_id", String, nullable=False),
    Column("terminal_chapter_baseline_id", String, nullable=False),
    Column("canon_baseline_id", String, nullable=False),
    Column("committed_chapter_count", Integer, nullable=False),
    Column("source_task_id", String, nullable=False),
    Column("completion_decision_ref_id", String, nullable=False),
    Column("gate_manifest_ref_id", String, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("book_id", "completion_version"),
    UniqueConstraint("project_id", "book_id", "id"),
    _ck(
        "version_parent",
        "((completion_version = 1 AND parent_completion_id IS NULL) "
        "OR (completion_version >= 2 AND parent_completion_id IS NOT NULL))",
    ),
    _ck("committed_chapter_count_positive", "committed_chapter_count >= 1"),
    ForeignKeyConstraint(
        ["project_id", "book_id", "parent_completion_id"],
        ["book_completions.project_id", "book_completions.book_id", "book_completions.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "terminal_arc_id", "terminal_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        [
            "project_id",
            "book_id",
            "terminal_arc_id",
            "terminal_chapter_id",
            "terminal_chapter_baseline_id",
        ],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "source_task_id"],
        ["agent_tasks.project_id", "agent_tasks.id"],
    ),
    *_content_ref_fks("completion_decision_ref_id", "gate_manifest_ref_id"),
)


# Arc lifecycle --------------------------------------------------------------------

arc_workspaces = Table(
    "arc_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("lock_version", Integer, nullable=False),
    Column("base_arc_baseline_id", String),
    Column("book_baseline_id", String, nullable=False),
    Column("canon_baseline_id", String, nullable=False),
    Column("prior_arc_id", String),
    Column("prior_arc_baseline_id", String),
    Column("plan_ref_id", String),
    Column("recommended_target_chapter_count", Integer),
    Column("guidance_ref_id", String),
    Column("repair_policy_id", String, nullable=False),
    Column("semantic_repair_count", Integer, nullable=False),
    Column("semantic_repair_limit", Integer, nullable=False),
    Column("stale_reason_code", String),
    Column("stale_at_ms", Integer),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("arc_id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "id"),
    *_workspace_common_checks(),
    _ck(
        "plan_count_pair",
        "((plan_ref_id IS NULL AND recommended_target_chapter_count IS NULL) "
        "OR (plan_ref_id IS NOT NULL AND recommended_target_chapter_count BETWEEN 1 AND 30))",
    ),
    _ck(
        "prior_arc_pair",
        "((prior_arc_id IS NULL AND prior_arc_baseline_id IS NULL) "
        "OR (prior_arc_id IS NOT NULL AND prior_arc_baseline_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id"],
        ["story_arcs.project_id", "story_arcs.book_id", "story_arcs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "base_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "prior_arc_id", "prior_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    *_content_ref_fks("plan_ref_id", "guidance_ref_id"),
)

arc_review_submissions = Table(
    "arc_review_submissions",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("workspace_id", String, nullable=False),
    Column("workspace_lock_version", Integer, nullable=False),
    Column("base_arc_baseline_id", String),
    Column("book_baseline_id", String, nullable=False),
    Column("canon_baseline_id", String, nullable=False),
    Column("prior_arc_id", String),
    Column("prior_arc_baseline_id", String),
    Column("purpose", String, nullable=False),
    Column("plan_ref_id", String, nullable=False),
    Column("recommended_target_chapter_count", Integer, nullable=False),
    Column("content_manifest_ref_id", String, nullable=False),
    Column("content_fingerprint", String, nullable=False),
    Column("disposition", String, nullable=False),
    Column("close_reason_code", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("closed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "book_id", "arc_id", "id"),
    _ck("workspace_lock_version_positive", "workspace_lock_version >= 1"),
    _ck(
        "recommended_count_range",
        "recommended_target_chapter_count BETWEEN 1 AND 30",
    ),
    _ck("content_fingerprint", _sha_expression("content_fingerprint")),
    _enum_ck("purpose", "purpose", ("regular", "final")),
    _enum_ck("disposition", "disposition", SUBMISSION_DISPOSITIONS),
    _submission_close_ck(),
    _ck(
        "prior_arc_pair",
        "((prior_arc_id IS NULL AND prior_arc_baseline_id IS NULL) "
        "OR (prior_arc_id IS NOT NULL AND prior_arc_baseline_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "workspace_id"],
        [
            "arc_workspaces.project_id",
            "arc_workspaces.book_id",
            "arc_workspaces.arc_id",
            "arc_workspaces.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "base_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "prior_arc_id", "prior_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "purpose"],
        [
            "story_arcs.project_id",
            "story_arcs.book_id",
            "story_arcs.id",
            "story_arcs.purpose",
        ],
    ),
    *_content_ref_fks("plan_ref_id", "content_manifest_ref_id"),
)
Index(
    "uq_submission_one_pending_per_arc",
    arc_review_submissions.c.arc_id,
    unique=True,
    sqlite_where=arc_review_submissions.c.disposition == "pending",
)

arc_reviews = Table(
    "arc_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("evaluator_task_id", String, nullable=False),
    Column("evaluator_attempt_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("rubric_id", String, nullable=False),
    Column("rubric_version", Integer, nullable=False),
    Column("precheck_ref_id", String, nullable=False),
    Column("detail_ref_id", String, nullable=False),
    Column("repair_contract_ref_id", String),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("submission_id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "submission_id", "id"),
    _enum_ck(
        "decision",
        "decision",
        ("pass", "local_repair", "escalate_to_book", "needs_user"),
    ),
    _ck("rubric_version_positive", "rubric_version >= 1"),
    _ck(
        "repair_contract",
        "((decision = 'local_repair' AND repair_contract_ref_id IS NOT NULL) "
        "OR (decision <> 'local_repair' AND repair_contract_ref_id IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "submission_id"],
        [
            "arc_review_submissions.project_id",
            "arc_review_submissions.book_id",
            "arc_review_submissions.arc_id",
            "arc_review_submissions.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "evaluator_task_id", "evaluator_attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
    ),
    *_content_ref_fks("precheck_ref_id", "detail_ref_id", "repair_contract_ref_id"),
)

arc_approval_gates = Table(
    "arc_approval_gates",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("closed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "submission_id", "review_id", "id"),
    _enum_ck("reason", "reason", ("participatory_mode", "mode_switch", "preserved_gate")),
    _enum_ck("state", "state", ("pending", "decided", "superseded")),
    _ck(
        "state_close_time",
        "((state = 'pending' AND closed_at_ms IS NULL) "
        "OR (state <> 'pending' AND closed_at_ms IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "submission_id", "review_id"],
        [
            "arc_reviews.project_id",
            "arc_reviews.book_id",
            "arc_reviews.arc_id",
            "arc_reviews.submission_id",
            "arc_reviews.id",
        ],
    ),
)
Index(
    "uq_arc_one_pending_approval_gate",
    arc_approval_gates.c.arc_id,
    unique=True,
    sqlite_where=arc_approval_gates.c.state == "pending",
)

arc_approvals = Table(
    "arc_approvals",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("gate_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("target_chapter_count", Integer),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("gate_id"),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint(
        "project_id",
        "book_id",
        "arc_id",
        "submission_id",
        "review_id",
        "gate_id",
        "id",
    ),
    _enum_ck("decision", "decision", ("approved", "rejected")),
    _ck(
        "approval_target_count",
        "((decision = 'approved' AND target_chapter_count BETWEEN 1 AND 30) "
        "OR (decision = 'rejected' AND target_chapter_count IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "submission_id", "review_id", "gate_id"],
        [
            "arc_approval_gates.project_id",
            "arc_approval_gates.book_id",
            "arc_approval_gates.arc_id",
            "arc_approval_gates.submission_id",
            "arc_approval_gates.review_id",
            "arc_approval_gates.id",
        ],
    ),
)

arc_baselines = Table(
    "arc_baselines",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("baseline_version", Integer, nullable=False),
    Column("parent_baseline_id", String),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("book_baseline_id", String, nullable=False),
    Column("canon_baseline_id", String, nullable=False),
    Column("prior_arc_id", String),
    Column("prior_arc_baseline_id", String),
    Column("purpose", String, nullable=False),
    Column("plan_ref_id", String, nullable=False),
    Column("recommended_target_chapter_count", Integer, nullable=False),
    Column("target_chapter_count", Integer, nullable=False),
    Column("authorization_kind", String, nullable=False),
    Column("approval_gate_id", String),
    Column("approval_id", String),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("arc_id", "baseline_version"),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint("project_id", "arc_id", "id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "id"),
    _ck(
        "version_parent",
        "((baseline_version = 1 AND parent_baseline_id IS NULL) "
        "OR (baseline_version >= 2 AND parent_baseline_id IS NOT NULL))",
    ),
    _ck(
        "chapter_count_ranges",
        "recommended_target_chapter_count BETWEEN 1 AND 30 "
        "AND target_chapter_count BETWEEN 1 AND 30",
    ),
    _ck(
        "prior_arc_pair",
        "((prior_arc_id IS NULL AND prior_arc_baseline_id IS NULL) "
        "OR (prior_arc_id IS NOT NULL AND prior_arc_baseline_id IS NOT NULL))",
    ),
    _enum_ck("purpose", "purpose", ("regular", "final")),
    _enum_ck("authorization_kind", "authorization_kind", ("policy_auto", "human_approval")),
    _ck(
        "authorization",
        "((authorization_kind = 'policy_auto' AND approval_gate_id IS NULL "
        "AND approval_id IS NULL AND target_chapter_count = recommended_target_chapter_count) "
        "OR (authorization_kind = 'human_approval' AND approval_gate_id IS NOT NULL "
        "AND approval_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "parent_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "submission_id", "review_id"],
        [
            "arc_reviews.project_id",
            "arc_reviews.book_id",
            "arc_reviews.arc_id",
            "arc_reviews.submission_id",
            "arc_reviews.id",
        ],
    ),
    ForeignKeyConstraint(
        [
            "project_id",
            "book_id",
            "arc_id",
            "submission_id",
            "review_id",
            "approval_gate_id",
            "approval_id",
        ],
        [
            "arc_approvals.project_id",
            "arc_approvals.book_id",
            "arc_approvals.arc_id",
            "arc_approvals.submission_id",
            "arc_approvals.review_id",
            "arc_approvals.gate_id",
            "arc_approvals.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "prior_arc_id", "prior_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "purpose"],
        [
            "story_arcs.project_id",
            "story_arcs.book_id",
            "story_arcs.id",
            "story_arcs.purpose",
        ],
    ),
    *_content_ref_fks("plan_ref_id"),
)


# Chapter/Canon lifecycle ----------------------------------------------------------

chapter_workspaces = Table(
    "chapter_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("chapter_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("lock_version", Integer, nullable=False),
    Column("base_chapter_baseline_id", String),
    Column("book_baseline_id", String, nullable=False),
    Column("arc_baseline_id", String, nullable=False),
    Column("canon_baseline_id", String, nullable=False),
    Column("plan_ref_id", String),
    Column("draft_ref_id", String),
    Column("observations_ref_id", String),
    Column("candidate_canon_patch_ref_id", String),
    Column("guidance_ref_id", String),
    Column("repair_policy_id", String, nullable=False),
    Column("semantic_repair_count", Integer, nullable=False),
    Column("semantic_repair_limit", Integer, nullable=False),
    Column("stale_reason_code", String),
    Column("stale_at_ms", Integer),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("chapter_id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "chapter_id", "id"),
    *_workspace_common_checks(),
    _ck(
        "component_dependencies",
        "(draft_ref_id IS NULL OR plan_ref_id IS NOT NULL) "
        "AND (observations_ref_id IS NULL OR draft_ref_id IS NOT NULL) "
        "AND (candidate_canon_patch_ref_id IS NULL OR observations_ref_id IS NOT NULL)",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id"],
        ["chapters.project_id", "chapters.book_id", "chapters.arc_id", "chapters.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "base_chapter_baseline_id"],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    *_content_ref_fks(
        "plan_ref_id",
        "draft_ref_id",
        "observations_ref_id",
        "candidate_canon_patch_ref_id",
        "guidance_ref_id",
    ),
)

chapter_review_submissions = Table(
    "chapter_review_submissions",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("chapter_id", String, nullable=False),
    Column("workspace_id", String, nullable=False),
    Column("workspace_lock_version", Integer, nullable=False),
    Column("base_chapter_baseline_id", String),
    Column("book_baseline_id", String, nullable=False),
    Column("arc_baseline_id", String, nullable=False),
    Column("canon_before_id", String, nullable=False),
    Column("plan_ref_id", String, nullable=False),
    Column("draft_ref_id", String, nullable=False),
    Column("observations_ref_id", String, nullable=False),
    Column("candidate_canon_patch_ref_id", String, nullable=False),
    Column("content_manifest_ref_id", String, nullable=False),
    Column("content_fingerprint", String, nullable=False),
    Column("disposition", String, nullable=False),
    Column("close_reason_code", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("closed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "book_id", "arc_id", "chapter_id", "id"),
    _ck("workspace_lock_version_positive", "workspace_lock_version >= 1"),
    _ck("content_fingerprint", _sha_expression("content_fingerprint")),
    _enum_ck("disposition", "disposition", SUBMISSION_DISPOSITIONS),
    _submission_close_ck(),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "workspace_id"],
        [
            "chapter_workspaces.project_id",
            "chapter_workspaces.book_id",
            "chapter_workspaces.arc_id",
            "chapter_workspaces.chapter_id",
            "chapter_workspaces.id",
        ],
    ),
    ForeignKeyConstraint(
        [
            "project_id",
            "book_id",
            "arc_id",
            "chapter_id",
            "base_chapter_baseline_id",
        ],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_before_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    *_content_ref_fks(
        "plan_ref_id",
        "draft_ref_id",
        "observations_ref_id",
        "candidate_canon_patch_ref_id",
        "content_manifest_ref_id",
    ),
)
Index(
    "uq_submission_one_pending_per_chapter",
    chapter_review_submissions.c.chapter_id,
    unique=True,
    sqlite_where=chapter_review_submissions.c.disposition == "pending",
)

chapter_reviews = Table(
    "chapter_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("chapter_id", String, nullable=False),
    Column("submission_id", String, nullable=False),
    Column("evaluator_task_id", String, nullable=False),
    Column("evaluator_attempt_id", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("rubric_id", String, nullable=False),
    Column("rubric_version", Integer, nullable=False),
    Column("precheck_ref_id", String, nullable=False),
    Column("detail_ref_id", String, nullable=False),
    Column("repair_contract_ref_id", String),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("submission_id"),
    UniqueConstraint(
        "project_id",
        "book_id",
        "arc_id",
        "chapter_id",
        "submission_id",
        "id",
    ),
    _enum_ck(
        "decision",
        "decision",
        ("pass", "local_repair", "escalate_to_arc", "escalate_to_book", "needs_user"),
    ),
    _ck("rubric_version_positive", "rubric_version >= 1"),
    _ck(
        "repair_contract",
        "((decision = 'local_repair' AND repair_contract_ref_id IS NOT NULL) "
        "OR (decision <> 'local_repair' AND repair_contract_ref_id IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "submission_id"],
        [
            "chapter_review_submissions.project_id",
            "chapter_review_submissions.book_id",
            "chapter_review_submissions.arc_id",
            "chapter_review_submissions.chapter_id",
            "chapter_review_submissions.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "evaluator_task_id", "evaluator_attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
    ),
    *_content_ref_fks("precheck_ref_id", "detail_ref_id", "repair_contract_ref_id"),
)

chapter_baselines = Table(
    "chapter_baselines",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("chapter_id", String, nullable=False),
    Column("baseline_version", Integer, nullable=False),
    Column("parent_baseline_id", String),
    Column("submission_id", String, nullable=False),
    Column("review_id", String, nullable=False),
    Column("book_baseline_id", String, nullable=False),
    Column("arc_baseline_id", String, nullable=False),
    Column("canon_before_id", String, nullable=False),
    Column("canon_after_id", String, nullable=False),
    Column("plan_ref_id", String, nullable=False),
    Column("prose_ref_id", String, nullable=False),
    Column("observations_ref_id", String, nullable=False),
    Column("accepted_canon_patch_ref_id", String, nullable=False),
    Column("chapter_title", String, nullable=False),
    Column("character_count", Integer, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("chapter_id", "baseline_version"),
    UniqueConstraint("submission_id"),
    UniqueConstraint("review_id"),
    UniqueConstraint("project_id", "book_id", "arc_id", "chapter_id", "id"),
    _ck(
        "version_parent",
        "((baseline_version = 1 AND parent_baseline_id IS NULL) "
        "OR (baseline_version >= 2 AND parent_baseline_id IS NOT NULL))",
    ),
    _ck("chapter_title_non_blank", _non_blank_expression("chapter_title")),
    _ck("character_count_non_negative", "character_count >= 0"),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "parent_baseline_id"],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "submission_id", "review_id"],
        [
            "chapter_reviews.project_id",
            "chapter_reviews.book_id",
            "chapter_reviews.arc_id",
            "chapter_reviews.chapter_id",
            "chapter_reviews.submission_id",
            "chapter_reviews.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_before_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_after_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    *_content_ref_fks(
        "plan_ref_id",
        "prose_ref_id",
        "observations_ref_id",
        "accepted_canon_patch_ref_id",
    ),
)

canon_baselines = Table(
    "canon_baselines",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("baseline_version", Integer, nullable=False),
    Column("parent_canon_baseline_id", String),
    Column("source_book_id", String),
    Column("source_arc_id", String),
    Column("source_chapter_id", String),
    Column("source_chapter_baseline_id", String),
    Column("accepted_patch_ref_id", String),
    Column("characters_ref_id", String, nullable=False),
    Column("relationships_ref_id", String, nullable=False),
    Column("world_facts_ref_id", String, nullable=False),
    Column("foreshadowing_ref_id", String, nullable=False),
    Column("manifest_fingerprint", String, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "baseline_version"),
    UniqueConstraint("source_chapter_baseline_id"),
    _ck("manifest_fingerprint", _sha_expression("manifest_fingerprint")),
    _ck(
        "seed_or_derived",
        "((baseline_version = 1 AND parent_canon_baseline_id IS NULL "
        "AND source_book_id IS NULL AND source_arc_id IS NULL "
        "AND source_chapter_id IS NULL AND source_chapter_baseline_id IS NULL "
        "AND accepted_patch_ref_id IS NULL) "
        "OR (baseline_version >= 2 AND parent_canon_baseline_id IS NOT NULL "
        "AND source_book_id IS NOT NULL AND source_arc_id IS NOT NULL "
        "AND source_chapter_id IS NOT NULL AND source_chapter_baseline_id IS NOT NULL "
        "AND accepted_patch_ref_id IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "parent_canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        [
            "project_id",
            "source_book_id",
            "source_arc_id",
            "source_chapter_id",
            "source_chapter_baseline_id",
        ],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    *_content_ref_fks(
        "accepted_patch_ref_id",
        "characters_ref_id",
        "relationships_ref_id",
        "world_facts_ref_id",
        "foreshadowing_ref_id",
    ),
)


# User feedback and explicit cross-layer change requests ---------------------------

user_feedback = Table(
    "user_feedback",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("content_ref_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("route_layer", String),
    Column("book_id", String),
    Column("arc_id", String),
    Column("chapter_id", String),
    Column("applied_command_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("routed_at_ms", Integer),
    Column("applied_at_ms", Integer),
    *_project_owned_constraints(),
    _enum_ck("status", "status", ("pending", "routed", "applied", "dismissed")),
    _ck(
        "route_target_shape",
        "((route_layer IS NULL AND book_id IS NULL AND arc_id IS NULL AND chapter_id IS NULL) "
        "OR (route_layer = 'book' AND book_id IS NOT NULL "
        "AND arc_id IS NULL AND chapter_id IS NULL) "
        "OR (route_layer = 'arc' AND book_id IS NOT NULL "
        "AND arc_id IS NOT NULL AND chapter_id IS NULL) "
        "OR (route_layer = 'chapter' AND book_id IS NOT NULL "
        "AND arc_id IS NOT NULL AND chapter_id IS NOT NULL))",
    ),
    _ck(
        "status_fields",
        "((status = 'pending' AND route_layer IS NULL AND routed_at_ms IS NULL "
        "AND applied_command_id IS NULL AND applied_at_ms IS NULL) "
        "OR (status = 'routed' AND route_layer IS NOT NULL AND routed_at_ms IS NOT NULL "
        "AND applied_command_id IS NULL AND applied_at_ms IS NULL) "
        "OR (status = 'applied' AND route_layer IS NOT NULL AND routed_at_ms IS NOT NULL "
        "AND applied_command_id IS NOT NULL AND applied_at_ms IS NOT NULL) "
        "OR (status = 'dismissed' AND applied_command_id IS NULL AND applied_at_ms IS NOT NULL "
        "AND ((route_layer IS NULL AND routed_at_ms IS NULL) "
        "OR (route_layer IS NOT NULL AND routed_at_ms IS NOT NULL))))",
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id"],
        ["books.project_id", "books.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id"],
        ["story_arcs.project_id", "story_arcs.book_id", "story_arcs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id"],
        ["chapters.project_id", "chapters.book_id", "chapters.arc_id", "chapters.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "applied_command_id"],
        ["command_receipts.project_id", "command_receipts.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    _content_ref_fk("content_ref_id"),
)


def _change_request_status_ck(resolved_column: str) -> CheckConstraint:
    return _ck(
        "status_resolution_fields",
        f"((status = 'open' AND {resolved_column} IS NULL "
        "AND close_reason_code IS NULL AND closed_at_ms IS NULL) "
        f"OR (status = 'resolved' AND {resolved_column} IS NOT NULL "
        "AND close_reason_code IS NOT NULL AND closed_at_ms IS NOT NULL) "
        f"OR (status IN ('rejected', 'superseded') AND {resolved_column} IS NULL "
        "AND close_reason_code IS NOT NULL AND closed_at_ms IS NOT NULL))",
    )


chapter_arc_change_requests = Table(
    "chapter_arc_change_requests",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String, nullable=False),
    Column("chapter_id", String, nullable=False),
    Column("source_submission_id", String, nullable=False),
    Column("source_review_id", String, nullable=False),
    Column("target_arc_baseline_id", String, nullable=False),
    Column("evidence_ref_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("resolved_by_arc_baseline_id", String),
    Column("close_reason_code", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("closed_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("source_review_id"),
    _enum_ck("status", "status", CHANGE_REQUEST_STATUSES),
    _change_request_status_ck("resolved_by_arc_baseline_id"),
    ForeignKeyConstraint(
        [
            "project_id",
            "book_id",
            "arc_id",
            "chapter_id",
            "source_submission_id",
            "source_review_id",
        ],
        [
            "chapter_reviews.project_id",
            "chapter_reviews.book_id",
            "chapter_reviews.arc_id",
            "chapter_reviews.chapter_id",
            "chapter_reviews.submission_id",
            "chapter_reviews.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "target_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "resolved_by_arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    _content_ref_fk("evidence_ref_id"),
)


def _book_change_request_table(name: str, *, source_layer: str) -> Table:
    source_columns: list[Column[Any]] = [
        Column("id", String, primary_key=True),
        Column("project_id", String, nullable=False),
        Column("book_id", String, nullable=False),
        Column("arc_id", String, nullable=False),
    ]
    if source_layer == "chapter":
        source_columns.append(Column("chapter_id", String, nullable=False))
    source_columns.extend(
        [
            Column("source_submission_id", String, nullable=False),
            Column("source_review_id", String, nullable=False),
            Column("target_book_baseline_id", String, nullable=False),
            Column("evidence_ref_id", String, nullable=False),
            Column("status", String, nullable=False),
            Column("resolved_by_book_baseline_id", String),
            Column("close_reason_code", String),
            Column("created_at_ms", Integer, nullable=False),
            Column("closed_at_ms", Integer),
        ]
    )
    if source_layer == "chapter":
        source_fk = ForeignKeyConstraint(
            [
                "project_id",
                "book_id",
                "arc_id",
                "chapter_id",
                "source_submission_id",
                "source_review_id",
            ],
            [
                "chapter_reviews.project_id",
                "chapter_reviews.book_id",
                "chapter_reviews.arc_id",
                "chapter_reviews.chapter_id",
                "chapter_reviews.submission_id",
                "chapter_reviews.id",
            ],
        )
    else:
        source_fk = ForeignKeyConstraint(
            ["project_id", "book_id", "arc_id", "source_submission_id", "source_review_id"],
            [
                "arc_reviews.project_id",
                "arc_reviews.book_id",
                "arc_reviews.arc_id",
                "arc_reviews.submission_id",
                "arc_reviews.id",
            ],
        )
    return Table(
        name,
        metadata,
        *source_columns,
        *_project_owned_constraints(),
        UniqueConstraint("source_review_id"),
        _enum_ck("status", "status", CHANGE_REQUEST_STATUSES),
        _change_request_status_ck("resolved_by_book_baseline_id"),
        source_fk,
        ForeignKeyConstraint(
            ["project_id", "book_id", "target_book_baseline_id"],
            ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "book_id", "resolved_by_book_baseline_id"],
            ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
        ),
        _content_ref_fk("evidence_ref_id"),
    )


chapter_book_change_requests = _book_change_request_table(
    "chapter_book_change_requests",
    source_layer="chapter",
)
arc_book_change_requests = _book_change_request_table(
    "arc_book_change_requests",
    source_layer="arc",
)


# Execution/control ----------------------------------------------------------------

generation_runs = Table(
    "generation_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("run_number", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("desired_state", String, nullable=False),
    Column("lock_version", Integer, nullable=False),
    Column("wait_reason_code", String),
    Column("blocking_task_id", String),
    Column("failure_code", String),
    Column("failure_ref_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("started_at_ms", Integer),
    Column("updated_at_ms", Integer, nullable=False),
    Column("finished_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "run_number"),
    _ck("run_number_lock_version", "run_number >= 1 AND lock_version >= 1"),
    _enum_ck(
        "status",
        "status",
        (
            "running",
            "pause_requested",
            "paused",
            "waiting_for_user",
            "failure_paused",
            "completed",
        ),
    ),
    _enum_ck("desired_state", "desired_state", ("running", "paused")),
    _ck(
        "completion_time",
        "((status = 'completed' AND finished_at_ms IS NOT NULL) "
        "OR (status <> 'completed' AND finished_at_ms IS NULL))",
    ),
    _ck(
        "status_desired_state",
        "((status = 'running' AND desired_state = 'running') "
        "OR (status = 'waiting_for_user' AND desired_state = 'running') "
        "OR (status IN ('pause_requested', 'paused', 'failure_paused', 'completed') "
        "AND desired_state = 'paused'))",
    ),
    _ck(
        "failure_pause_fields",
        "((status = 'failure_paused' AND blocking_task_id IS NOT NULL "
        "AND failure_code IS NOT NULL AND failure_ref_id IS NOT NULL) "
        "OR (status <> 'failure_paused' AND blocking_task_id IS NULL "
        "AND failure_code IS NULL AND failure_ref_id IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "blocking_task_id"],
        ["agent_tasks.project_id", "agent_tasks.id"],
    ),
    _content_ref_fk("failure_ref_id"),
)
Index(
    "uq_generation_run_one_open_per_project",
    generation_runs.c.project_id,
    unique=True,
    sqlite_where=generation_runs.c.finished_at_ms.is_(None),
)

engine_slot = Table(
    "engine_slot",
    metadata,
    Column("slot_id", Integer, primary_key=True, server_default=text("1")),
    Column("active_run_id", String),
    Column("owner_instance_id", String),
    Column("lease_token", String),
    Column("lease_expires_at_ms", Integer),
    Column("heartbeat_at_ms", Integer),
    Column("lock_version", Integer, nullable=False),
    _ck("single_slot", "slot_id = 1"),
    _ck("lock_version_positive", "lock_version >= 1"),
    _ck(
        "lease_fields",
        "((active_run_id IS NULL AND owner_instance_id IS NULL AND lease_token IS NULL "
        "AND lease_expires_at_ms IS NULL AND heartbeat_at_ms IS NULL) "
        "OR (active_run_id IS NOT NULL AND owner_instance_id IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at_ms IS NOT NULL "
        "AND heartbeat_at_ms IS NOT NULL))",
    ),
    ForeignKeyConstraint(
        ["active_run_id"],
        ["generation_runs.id"],
    ),
)

agent_tasks = Table(
    "agent_tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("task_key", String, nullable=False),
    Column("action_key", String, nullable=False),
    Column("predecessor_task_id", String),
    Column("role", String, nullable=False),
    Column("task_kind", String, nullable=False),
    Column("scope_layer", String, nullable=False),
    Column("book_id", String, nullable=False),
    Column("arc_id", String),
    Column("chapter_id", String),
    Column("workspace_lock_version", Integer),
    Column("book_baseline_id", String),
    Column("arc_baseline_id", String),
    Column("chapter_baseline_id", String),
    Column("canon_baseline_id", String, nullable=False),
    Column("task_plan_ref_id", String, nullable=False),
    Column("input_manifest_ref_id", String, nullable=False),
    Column("input_messages_ref_id", String, nullable=False),
    Column("profile_snapshot_ref_id", String, nullable=False),
    Column("input_fingerprint", String, nullable=False),
    Column("prompt_fingerprint", String, nullable=False),
    Column("context_policy_id", String, nullable=False),
    Column("context_policy_version", Integer, nullable=False),
    Column("context_policy_fingerprint", String, nullable=False),
    Column("output_schema_id", String, nullable=False),
    Column("output_schema_version", Integer, nullable=False),
    Column("output_schema_fingerprint", String, nullable=False),
    Column("rubric_id", String),
    Column("rubric_version", Integer),
    Column("harness_policy_id", String, nullable=False),
    Column("harness_policy_version", Integer, nullable=False),
    Column("profile_id", String, nullable=False),
    Column("profile_fingerprint", String, nullable=False),
    Column("api_family", String, nullable=False),
    Column("model_id", String, nullable=False),
    Column("output_mode", String, nullable=False),
    Column("requires_native_json_schema", Integer, nullable=False),
    Column("requires_text_streaming", Integer, nullable=False),
    Column("transport_retry_limit", Integer, nullable=False),
    Column("model_request_limit", Integer, nullable=False),
    Column("connect_timeout_ms", Integer, nullable=False),
    Column("pool_timeout_ms", Integer, nullable=False),
    Column("write_timeout_ms", Integer, nullable=False),
    Column("read_timeout_ms", Integer, nullable=False),
    Column("activation_timeout_ms", Integer, nullable=False),
    Column("timeout_policy_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("successful_attempt_id", String),
    Column("delivery_state", String, nullable=False),
    Column("applied_command_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "task_key"),
    _ck(
        "identity_strings_non_blank",
        f"{_non_blank_expression('task_key')} AND {_non_blank_expression('action_key')} "
        f"AND {_non_blank_expression('task_kind')}",
    ),
    _enum_ck(
        "role",
        "role",
        ("book_strategist", "arc_planner", "chapter_writer", "evaluator"),
    ),
    _enum_ck("scope_layer", "scope_layer", ("book", "arc", "chapter")),
    _ck(
        "scope_ids",
        "((scope_layer = 'book' AND arc_id IS NULL AND chapter_id IS NULL) "
        "OR (scope_layer = 'arc' AND arc_id IS NOT NULL AND chapter_id IS NULL) "
        "OR (scope_layer = 'chapter' AND arc_id IS NOT NULL AND chapter_id IS NOT NULL))",
    ),
    _ck("workspace_lock_version", "workspace_lock_version IS NULL OR workspace_lock_version >= 1"),
    _ck(
        "scope_baselines",
        "((scope_layer = 'book' AND arc_baseline_id IS NULL AND chapter_baseline_id IS NULL) "
        "OR (scope_layer = 'arc' AND book_baseline_id IS NOT NULL "
        "AND chapter_baseline_id IS NULL) "
        "OR (scope_layer = 'chapter' AND book_baseline_id IS NOT NULL "
        "AND arc_baseline_id IS NOT NULL))",
    ),
    _ck(
        "fingerprints",
        f"{_sha_expression('input_fingerprint')} "
        f"AND {_sha_expression('prompt_fingerprint')} "
        f"AND {_sha_expression('context_policy_fingerprint')} "
        f"AND {_sha_expression('output_schema_fingerprint')} "
        f"AND {_sha_expression('profile_fingerprint')}",
    ),
    _ck(
        "policy_versions_positive",
        "context_policy_version >= 1 AND output_schema_version >= 1 "
        "AND harness_policy_version >= 1",
    ),
    _ck(
        "rubric_pair",
        "((rubric_id IS NULL AND rubric_version IS NULL) "
        "OR (rubric_id IS NOT NULL AND length(trim(rubric_id)) > 0 "
        "AND rubric_version >= 1))",
    ),
    _enum_ck(
        "api_family",
        "api_family",
        ("openai_responses", "openai_chat_completions", "anthropic_messages"),
    ),
    _enum_ck("output_mode", "output_mode", ("native_json_schema", "text_streaming")),
    _ck(
        "required_capability_flags",
        "requires_native_json_schema IN (0, 1) AND requires_text_streaming IN (0, 1) "
        "AND ((output_mode = 'native_json_schema' AND requires_native_json_schema = 1 "
        "AND requires_text_streaming = 0) "
        "OR (output_mode = 'text_streaming' AND requires_native_json_schema = 0 "
        "AND requires_text_streaming = 1))",
    ),
    _ck(
        "fixed_retry_and_timeouts",
        "transport_retry_limit = 5 AND connect_timeout_ms = 10000 "
        "AND pool_timeout_ms = 10000 AND write_timeout_ms = 60000 "
        "AND read_timeout_ms = 600000 AND activation_timeout_ms = 1800000 "
        "AND timeout_policy_id = 'provider-timeout-t1-v1'",
    ),
    _ck(
        "model_request_limit",
        "((output_mode = 'text_streaming' AND model_request_limit = 1) "
        "OR (output_mode = 'native_json_schema' AND model_request_limit = 2))",
    ),
    _enum_ck("status", "status", ("queued", "running", "succeeded", "failed", "superseded")),
    _enum_ck(
        "delivery_state",
        "delivery_state",
        ("not_ready", "pending", "applied", "discarded_stale", "failed"),
    ),
    _ck(
        "success_delivery",
        "((status = 'succeeded' AND successful_attempt_id IS NOT NULL "
        "AND delivery_state IN ('pending', 'applied', 'discarded_stale')) "
        "OR (status = 'failed' AND successful_attempt_id IS NULL "
        "AND delivery_state IN ('not_ready', 'failed')) "
        "OR (status NOT IN ('succeeded', 'failed') AND successful_attempt_id IS NULL "
        "AND delivery_state = 'not_ready'))",
    ),
    _ck(
        "applied_command",
        "((delivery_state = 'applied' AND applied_command_id IS NOT NULL) "
        "OR (delivery_state <> 'applied' AND applied_command_id IS NULL))",
    ),
    ForeignKeyConstraint(
        ["project_id", "run_id"],
        ["generation_runs.project_id", "generation_runs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id"],
        ["books.project_id", "books.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id"],
        ["story_arcs.project_id", "story_arcs.book_id", "story_arcs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id"],
        ["chapters.project_id", "chapters.book_id", "chapters.arc_id", "chapters.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "book_baseline_id"],
        ["book_baselines.project_id", "book_baselines.book_id", "book_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "arc_baseline_id"],
        [
            "arc_baselines.project_id",
            "arc_baselines.book_id",
            "arc_baselines.arc_id",
            "arc_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "book_id", "arc_id", "chapter_id", "chapter_baseline_id"],
        [
            "chapter_baselines.project_id",
            "chapter_baselines.book_id",
            "chapter_baselines.arc_id",
            "chapter_baselines.chapter_id",
            "chapter_baselines.id",
        ],
    ),
    ForeignKeyConstraint(
        ["project_id", "canon_baseline_id"],
        ["canon_baselines.project_id", "canon_baselines.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "predecessor_task_id"],
        ["agent_tasks.project_id", "agent_tasks.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "applied_command_id"],
        ["command_receipts.project_id", "command_receipts.id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["project_id", "id", "successful_attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    *_content_ref_fks(
        "task_plan_ref_id",
        "input_manifest_ref_id",
        "input_messages_ref_id",
        "profile_snapshot_ref_id",
    ),
)

agent_task_attempts = Table(
    "agent_task_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("retry_kind", String, nullable=False),
    Column("predecessor_attempt_id", String),
    Column("status", String, nullable=False),
    Column("owner_instance_id", String),
    Column("lease_token", String),
    Column("lease_expires_at_ms", Integer),
    Column("heartbeat_at_ms", Integer),
    Column("activation_deadline_at_ms", Integer),
    Column("framework_fingerprint", String, nullable=False),
    Column("provider_request_identity", String),
    Column("provider_request_count", Integer, nullable=False),
    Column("transport_retry_count", Integer, nullable=False),
    Column("model_request_count", Integer, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("total_tokens", Integer),
    Column("usage_ref_id", String),
    Column("result_ref_id", String),
    Column("error_code", String),
    Column("error_category", String),
    Column("http_status", Integer),
    Column("error_ref_id", String),
    Column("diagnostic_ref_id", String),
    Column("created_at_ms", Integer, nullable=False),
    Column("started_at_ms", Integer),
    Column("finished_at_ms", Integer),
    *_project_owned_constraints(),
    UniqueConstraint("task_id", "attempt_number"),
    UniqueConstraint("project_id", "task_id", "id"),
    _ck("attempt_number_positive", "attempt_number >= 1"),
    _enum_ck("retry_kind", "retry_kind", ("initial", "crash_replay", "user_retry")),
    _enum_ck(
        "status",
        "status",
        ("queued", "running", "succeeded", "failed", "interrupted", "delivery_failed"),
    ),
    _ck("framework_fingerprint", _sha_expression("framework_fingerprint")),
    _ck(
        "request_counters",
        "provider_request_count >= 0 AND transport_retry_count >= 0 "
        "AND model_request_count >= 0 "
        "AND provider_request_count = model_request_count + transport_retry_count "
        "AND provider_request_count <= 6 AND transport_retry_count <= 5",
    ),
    _ck(
        "usage_tokens",
        "((input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL) "
        "OR (input_tokens >= 0 AND output_tokens >= 0 "
        "AND total_tokens = input_tokens + output_tokens))",
    ),
    _ck(
        "queued_fields",
        "status <> 'queued' OR (owner_instance_id IS NULL AND lease_token IS NULL "
        "AND lease_expires_at_ms IS NULL AND heartbeat_at_ms IS NULL "
        "AND activation_deadline_at_ms IS NULL AND started_at_ms IS NULL "
        "AND finished_at_ms IS NULL AND result_ref_id IS NULL "
        "AND error_code IS NULL AND error_category IS NULL AND error_ref_id IS NULL)",
    ),
    _ck(
        "running_fields",
        "status <> 'running' OR (owner_instance_id IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at_ms IS NOT NULL AND heartbeat_at_ms IS NOT NULL "
        "AND activation_deadline_at_ms IS NOT NULL AND started_at_ms IS NOT NULL "
        "AND finished_at_ms IS NULL AND result_ref_id IS NULL "
        "AND error_code IS NULL AND error_category IS NULL AND error_ref_id IS NULL)",
    ),
    _ck(
        "succeeded_fields",
        "status <> 'succeeded' OR (result_ref_id IS NOT NULL AND finished_at_ms IS NOT NULL "
        "AND error_code IS NULL AND error_category IS NULL AND error_ref_id IS NULL)",
    ),
    _ck(
        "failed_fields",
        "status <> 'failed' OR (result_ref_id IS NULL AND finished_at_ms IS NOT NULL "
        "AND error_code IS NOT NULL AND error_category IS NOT NULL AND error_ref_id IS NOT NULL)",
    ),
    _ck(
        "delivery_failed_fields",
        "status <> 'delivery_failed' OR (result_ref_id IS NOT NULL "
        "AND finished_at_ms IS NOT NULL AND error_code IS NOT NULL "
        "AND error_category = 'domain_delivery' AND error_ref_id IS NOT NULL "
        "AND owner_instance_id IS NULL AND lease_token IS NULL "
        "AND lease_expires_at_ms IS NULL AND heartbeat_at_ms IS NULL)",
    ),
    _ck(
        "interrupted_fields",
        "status <> 'interrupted' OR (result_ref_id IS NULL AND finished_at_ms IS NOT NULL)",
    ),
    ForeignKeyConstraint(
        ["project_id", "task_id"],
        ["agent_tasks.project_id", "agent_tasks.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "task_id", "predecessor_attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
    ),
    *_content_ref_fks(
        "usage_ref_id",
        "result_ref_id",
        "error_ref_id",
        "diagnostic_ref_id",
    ),
)
Index(
    "uq_attempt_one_running_per_task",
    agent_task_attempts.c.task_id,
    unique=True,
    sqlite_where=agent_task_attempts.c.status == "running",
)
Index(
    "uq_attempt_one_succeeded_per_task",
    agent_task_attempts.c.task_id,
    unique=True,
    sqlite_where=agent_task_attempts.c.status == "succeeded",
)
Index(
    "uq_attempt_one_initial_per_task",
    agent_task_attempts.c.task_id,
    unique=True,
    sqlite_where=agent_task_attempts.c.retry_kind == "initial",
)
Index(
    "uq_attempt_one_crash_replay_per_task",
    agent_task_attempts.c.task_id,
    unique=True,
    sqlite_where=agent_task_attempts.c.retry_kind == "crash_replay",
)

agent_evidence_items = Table(
    "agent_evidence_items",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("attempt_id", String, nullable=False),
    Column("sequence_number", Integer, nullable=False),
    Column("item_kind", String, nullable=False),
    Column("content_ref_id", String),
    Column("metadata_json", Text),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("attempt_id", "sequence_number"),
    _ck("sequence_number_positive", "sequence_number >= 1"),
    _enum_ck(
        "item_kind",
        "item_kind",
        (
            "message",
            "tool_call",
            "tool_result",
            "validation",
            "transport_retry",
            "model_retry",
            "completion_message",
            "diagnostic_attachment",
        ),
    ),
    _ck("metadata_json", "metadata_json IS NULL OR json_valid(metadata_json)"),
    ForeignKeyConstraint(
        ["project_id", "task_id", "attempt_id"],
        [
            "agent_task_attempts.project_id",
            "agent_task_attempts.task_id",
            "agent_task_attempts.id",
        ],
    ),
    _content_ref_fk("content_ref_id"),
)


# Command receipts and transactional event feed -----------------------------------

command_receipts = Table(
    "command_receipts",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, nullable=False),
    Column("run_id", String),
    Column("idempotency_key", String, nullable=False),
    Column("command_kind", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("request_fingerprint", String, nullable=False),
    Column("source_task_id", String),
    Column("result_json", Text, nullable=False),
    Column("first_event_sequence", Integer),
    Column("last_event_sequence", Integer),
    Column("created_at_ms", Integer, nullable=False),
    *_project_owned_constraints(),
    UniqueConstraint("project_id", "idempotency_key"),
    _ck(
        "identity_strings_non_blank",
        f"{_non_blank_expression('idempotency_key')} AND {_non_blank_expression('command_kind')}",
    ),
    _enum_ck("actor", "actor", ("user", "engine", "system")),
    _ck("request_fingerprint", _sha_expression("request_fingerprint")),
    _ck("result_json", "json_valid(result_json)"),
    _ck(
        "event_sequence_pair",
        "((first_event_sequence IS NULL AND last_event_sequence IS NULL) "
        "OR (first_event_sequence >= 1 AND last_event_sequence >= first_event_sequence))",
    ),
    ForeignKeyConstraint(
        ["project_id", "run_id"],
        ["generation_runs.project_id", "generation_runs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "source_task_id"],
        ["agent_tasks.project_id", "agent_tasks.id"],
    ),
)

domain_events = Table(
    "domain_events",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String, nullable=False),
    Column("project_id", String, nullable=False),
    Column("run_id", String),
    Column("command_receipt_id", String),
    Column("event_type", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("aggregate_type", String, nullable=False),
    Column("aggregate_id", String, nullable=False),
    Column("causation_id", String),
    Column("correlation_id", String),
    Column("payload_json", Text, nullable=False),
    Column("occurred_at_ms", Integer, nullable=False),
    UniqueConstraint("event_id"),
    _project_owner_fk(),
    _ck("schema_version_positive", "schema_version >= 1"),
    _ck(
        "descriptors_non_blank",
        f"{_non_blank_expression('event_type')} "
        f"AND {_non_blank_expression('aggregate_type')} "
        f"AND {_non_blank_expression('aggregate_id')}",
    ),
    _ck("payload_json", "json_valid(payload_json)"),
    ForeignKeyConstraint(
        ["project_id", "run_id"],
        ["generation_runs.project_id", "generation_runs.id"],
    ),
    ForeignKeyConstraint(
        ["project_id", "command_receipt_id"],
        ["command_receipts.project_id", "command_receipts.id"],
    ),
    sqlite_autoincrement=True,
)


# Required query and child-FK indexes ---------------------------------------------

Index(
    "ix_story_arcs_book_status_ordinal",
    story_arcs.c.book_id,
    story_arcs.c.lifecycle_status,
    story_arcs.c.ordinal,
)
Index(
    "ix_chapters_book_status_ordinal",
    chapters.c.book_id,
    chapters.c.lifecycle_status,
    chapters.c.book_ordinal,
)
Index(
    "ix_chapters_arc_status_ordinal",
    chapters.c.arc_id,
    chapters.c.lifecycle_status,
    chapters.c.arc_ordinal,
)
Index("ix_book_workspaces_project_state", book_workspaces.c.project_id, book_workspaces.c.state)
Index("ix_arc_workspaces_project_state", arc_workspaces.c.project_id, arc_workspaces.c.state)
Index(
    "ix_chapter_workspaces_project_state",
    chapter_workspaces.c.project_id,
    chapter_workspaces.c.state,
)
Index(
    "ix_book_submissions_scope_disposition_created",
    book_review_submissions.c.book_id,
    book_review_submissions.c.disposition,
    book_review_submissions.c.created_at_ms,
)
Index(
    "ix_arc_submissions_scope_disposition_created",
    arc_review_submissions.c.arc_id,
    arc_review_submissions.c.disposition,
    arc_review_submissions.c.created_at_ms,
)
Index(
    "ix_chapter_submissions_scope_disposition_created",
    chapter_review_submissions.c.chapter_id,
    chapter_review_submissions.c.disposition,
    chapter_review_submissions.c.created_at_ms,
)
Index(
    "ix_book_baselines_version_desc",
    book_baselines.c.book_id,
    book_baselines.c.baseline_version.desc(),
)
Index(
    "ix_arc_baselines_version_desc", arc_baselines.c.arc_id, arc_baselines.c.baseline_version.desc()
)
Index(
    "ix_chapter_baselines_version_desc",
    chapter_baselines.c.chapter_id,
    chapter_baselines.c.baseline_version.desc(),
)
Index(
    "ix_canon_baselines_project_version_desc",
    canon_baselines.c.project_id,
    canon_baselines.c.baseline_version.desc(),
)
Index(
    "ix_user_feedback_project_status_created",
    user_feedback.c.project_id,
    user_feedback.c.status,
    user_feedback.c.created_at_ms,
)
Index(
    "ix_chapter_arc_changes_target_status_created",
    chapter_arc_change_requests.c.arc_id,
    chapter_arc_change_requests.c.status,
    chapter_arc_change_requests.c.created_at_ms,
)
Index(
    "ix_chapter_book_changes_target_status_created",
    chapter_book_change_requests.c.book_id,
    chapter_book_change_requests.c.status,
    chapter_book_change_requests.c.created_at_ms,
)
Index(
    "ix_arc_book_changes_target_status_created",
    arc_book_change_requests.c.book_id,
    arc_book_change_requests.c.status,
    arc_book_change_requests.c.created_at_ms,
)
Index(
    "ix_generation_runs_project_status_updated",
    generation_runs.c.project_id,
    generation_runs.c.status,
    generation_runs.c.updated_at_ms,
)
Index(
    "ix_agent_tasks_run_status_created",
    agent_tasks.c.run_id,
    agent_tasks.c.status,
    agent_tasks.c.created_at_ms,
)
Index("ix_agent_tasks_project_action", agent_tasks.c.project_id, agent_tasks.c.action_key)
Index(
    "ix_agent_attempts_task_status_number",
    agent_task_attempts.c.task_id,
    agent_task_attempts.c.status,
    agent_task_attempts.c.attempt_number,
)
Index(
    "ix_agent_attempts_status_lease_expiry",
    agent_task_attempts.c.status,
    agent_task_attempts.c.lease_expires_at_ms,
)
Index(
    "ix_agent_evidence_attempt_sequence",
    agent_evidence_items.c.attempt_id,
    agent_evidence_items.c.sequence_number,
)
Index(
    "ix_command_receipts_project_created",
    command_receipts.c.project_id,
    command_receipts.c.created_at_ms,
)
Index("ix_domain_events_project_sequence", domain_events.c.project_id, domain_events.c.sequence)
Index("ix_domain_events_run_sequence", domain_events.c.run_id, domain_events.c.sequence)
Index(
    "ix_domain_events_aggregate_sequence",
    domain_events.c.aggregate_type,
    domain_events.c.aggregate_id,
    domain_events.c.sequence,
)


def _indexed_column_prefixes(table: Table) -> Iterable[tuple[str, ...]]:
    for constraint in table.constraints:
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)):
            yield tuple(column.name for column in constraint.columns)
    for index in table.indexes:
        names = tuple(
            expression.name for expression in index.expressions if isinstance(expression, Column)
        )
        if len(names) == len(index.expressions):
            yield names


def _add_missing_child_fk_indexes() -> None:
    for table in metadata.tables.values():
        indexed_prefixes = list(_indexed_column_prefixes(table))
        foreign_keys = sorted(
            table.foreign_key_constraints,
            key=lambda constraint: (
                -len(constraint.columns),
                tuple(column.name for column in constraint.columns),
            ),
        )
        for foreign_key in foreign_keys:
            column_names = tuple(column.name for column in foreign_key.columns)
            if any(prefix[: len(column_names)] == column_names for prefix in indexed_prefixes):
                continue
            index_name = f"ix_{table.name}_{'_'.join(column_names)}"
            if index_name in {index.name for index in table.indexes}:
                continue
            Index(index_name, *(table.c[column_name] for column_name in column_names))
            indexed_prefixes.append(column_names)


_add_missing_child_fk_indexes()

EXPECTED_TABLE_NAMES = frozenset(
    {
        "projects",
        "books",
        "story_arcs",
        "chapters",
        "content_blobs",
        "content_refs",
        "book_workspaces",
        "book_review_submissions",
        "book_reviews",
        "book_approvals",
        "book_baselines",
        "book_completions",
        "arc_workspaces",
        "arc_review_submissions",
        "arc_reviews",
        "arc_approval_gates",
        "arc_approvals",
        "arc_baselines",
        "chapter_workspaces",
        "chapter_review_submissions",
        "chapter_reviews",
        "chapter_baselines",
        "canon_baselines",
        "user_feedback",
        "chapter_arc_change_requests",
        "chapter_book_change_requests",
        "arc_book_change_requests",
        "generation_runs",
        "engine_slot",
        "agent_tasks",
        "agent_task_attempts",
        "agent_evidence_items",
        "command_receipts",
        "domain_events",
    }
)

if frozenset(metadata.tables) != EXPECTED_TABLE_NAMES:
    raise RuntimeError("LT1 metadata table catalog drifted from the approved 34-table topology.")
