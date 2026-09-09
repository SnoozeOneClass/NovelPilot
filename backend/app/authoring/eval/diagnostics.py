from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict

from app.authoring.domain.models import RunStatus
from app.authoring.store import AuthoringStore


class DiagnosticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: str
    code: str
    message: str
    evidence: dict[str, object]


class DiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: str
    findings: tuple[DiagnosticFinding, ...]
    style_metrics: dict[str, object]


async def diagnose(store: AuthoringStore, project_id: str) -> DiagnosticReport:
    project = await store.project(project_id)
    findings: list[DiagnosticFinding] = []
    if project.status is not RunStatus.COMPLETED:
        findings.append(
            DiagnosticFinding(
                severity="FAIL",
                code="run_not_completed",
                message="The authoring run did not reach its durable completed state.",
                evidence={"status": project.status.value},
            )
        )
    if project.chapter_count != project.target.target_chapters:
        findings.append(
            DiagnosticFinding(
                severity="FAIL",
                code="target_mismatch",
                message="Committed chapter count differs from the frozen target.",
                evidence={
                    "chapters": project.chapter_count,
                    "target": project.target.target_chapters,
                },
            )
        )
    episodes_without_terminal = int(
        await store.scalar(
            "SELECT count(*) FROM worker_episodes e WHERE e.project_id=? "
            "AND e.status='completed' AND e.instruction_kind<>'' AND NOT EXISTS ("
            "SELECT 1 FROM checkpoints c WHERE c.project_id=e.project_id "
            "AND c.instruction_key=e.instruction_key AND c.logical_target=e.logical_target "
            "AND c.step=CASE e.instruction_kind "
            "WHEN 'create_foundation' THEN 'audit_foundation' "
            "WHEN 'write_chapter' THEN 'commit_chapter' "
            "WHEN 'rewrite_chapter' THEN 'commit_chapter' "
            "WHEN 'review_boundary' THEN 'save_review' "
            "WHEN 'save_summary' THEN 'save_arc_summary' "
            "WHEN 'extend_outline' THEN 'revise_outline' "
            "WHEN 'complete_book' THEN 'complete_book' END)",
            (project_id,),
        )
        or 0
    )
    if episodes_without_terminal:
        findings.append(
            DiagnosticFinding(
                severity="FAIL",
                code="terminal_checkpoint_missing",
                message="A completed Worker episode lacks its durable terminal checkpoint.",
                evidence={"episode_count": episodes_without_terminal},
            )
        )

    endings: list[str] = []
    openings: list[str] = []
    titles: list[str] = []
    sentence_lengths: list[int] = []
    dialogue_characters = 0
    total_characters = 0
    for number in range(1, project.chapter_count + 1):
        chapter = await store.chapter(project_id, number)
        titles.append(str(chapter["title"]))
        sentences = [
            item.strip() for item in str(chapter["content"]).replace("！", "。").split("。")
        ]
        usable = [item for item in sentences if item]
        if usable:
            openings.append(usable[0][:12])
            sentence_lengths.extend(len(item) for item in usable)
        content = str(chapter["content"])
        dialogue_characters += content.count("“") + content.count('"')
        total_characters += len(content)
        endings.append(next((item for item in reversed(sentences) if item), ""))
    repeated_endings = sum(count - 1 for count in Counter(endings).values() if count > 1)
    duplicate_titles = len(titles) - len(set(titles))
    repeated_openings = sum(count - 1 for count in Counter(openings).values() if count > 1)
    title_format_violations = sum(not title.strip() for title in titles)
    if repeated_endings:
        findings.append(
            DiagnosticFinding(
                severity="WARN",
                code="repeated_chapter_endings",
                message="Multiple chapters use an identical ending sentence.",
                evidence={"duplicates": repeated_endings},
            )
        )
    if duplicate_titles:
        findings.append(
            DiagnosticFinding(
                severity="WARN",
                code="duplicate_chapter_titles",
                message="Multiple committed chapters use the same title.",
                evidence={"duplicates": duplicate_titles},
            )
        )
    verdict = (
        "FAIL"
        if any(item.severity == "FAIL" for item in findings)
        else ("WARN" if findings else "PASS")
    )
    return DiagnosticReport(
        verdict=verdict,
        findings=tuple(findings),
        style_metrics={
            "chapters": project.chapter_count,
            "duplicate_titles": duplicate_titles,
            "repeated_endings": repeated_endings,
            "repeated_openings": repeated_openings,
            "title_format_violations": title_format_violations,
            "average_sentence_length": (
                0
                if not sentence_lengths
                else round(sum(sentence_lengths) / len(sentence_lengths), 2)
            ),
            "dialogue_marker_ratio": (
                0 if not total_characters else round(dialogue_characters / total_characters, 4)
            ),
        },
    )
