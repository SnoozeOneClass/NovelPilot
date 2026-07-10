import type { ArtifactSummary, HarnessEvent } from "../../types/domain";

export interface MarkdownSection {
  title: string;
  body: string;
}

export interface CanonDocument {
  schema_version: number;
  version: number;
  items: Record<string, Record<string, unknown>>;
}

export const chapterPipeline = [
  { id: "assemble_context", label: "装配上下文" },
  { id: "generate_chapter_goal", label: "生成目标" },
  { id: "draft_chapter", label: "候选正文" },
  { id: "semantic_review", label: "语义审查" },
  { id: "verify_chapter", label: "章节验证" },
  { id: "commit_state_patch", label: "提交状态" }
] as const;

export const canonFiles = {
  characters: "canon/characters.json",
  relationships: "canon/relationships.json",
  world_facts: "canon/world_facts.json",
  foreshadowing: "canon/foreshadowing.json"
} as const;

export type CanonKind = keyof typeof canonFiles;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseJsonRecord(content: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(content);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function parseCanonDocument(content: string): CanonDocument {
  const parsed = parseJsonRecord(content);
  const rawItems = parsed?.items;
  const items: Record<string, Record<string, unknown>> = {};
  if (isRecord(rawItems)) {
    for (const [id, value] of Object.entries(rawItems)) {
      items[id] = isRecord(value) ? value : { value };
    }
  }
  return {
    schema_version: typeof parsed?.schema_version === "number" ? parsed.schema_version : 1,
    version: typeof parsed?.version === "number" ? parsed.version : 1,
    items
  };
}

export function parseMarkdownSections(markdown: string): MarkdownSection[] {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const sections: MarkdownSection[] = [];
  let title = "计划摘要";
  let body: string[] = [];

  const flush = () => {
    const text = body.join("\n").trim();
    if (text) {
      sections.push({ title, body: text });
    }
    body = [];
  };

  for (const line of lines) {
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) {
      flush();
      title = heading[1].trim();
      continue;
    }
    body.push(line);
  }
  flush();
  return sections;
}

export function formatClock(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return timestamp;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

export function chapterIdsFromArtifacts(paths: string[]): string[] {
  const ids = new Set<string>();
  for (const path of paths) {
    const match = path.match(/^chapters\/([^/]+)\//);
    if (match) ids.add(match[1]);
  }
  return [...ids].sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
}

export function arcIdsFromArtifacts(paths: string[]): string[] {
  const ids = new Set<string>();
  for (const path of paths) {
    const match = path.match(/^arcs\/([^/]+)\//);
    if (match) ids.add(match[1]);
  }
  return [...ids].sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
}

export function pipelineState(
  stepId: string,
  latestEvent: HarnessEvent | null,
  events: HarnessEvent[]
): "done" | "active" | "pending" {
  const latestIndex = chapterPipeline.findIndex((step) => step.id === latestEvent?.atomic_action);
  const stepIndex = chapterPipeline.findIndex((step) => step.id === stepId);
  const completed = events.some(
    (event) =>
      event.atomic_action === stepId &&
      event.status === "completed" &&
      event.kind !== "llm_output_delta"
  );
  if (latestEvent?.atomic_action === stepId && latestEvent.status !== "completed") return "active";
  if (completed || (latestIndex >= 0 && stepIndex < latestIndex)) return "done";
  return "pending";
}

export function artifactForChapter(
  summaries: ArtifactSummary[],
  chapterId: string | null,
  fileName: string
): ArtifactSummary | null {
  if (!chapterId) return null;
  return summaries.find((item) => item.path === `chapters/${chapterId}/${fileName}`) ?? null;
}

export function firstTextValue(value: Record<string, unknown>): string {
  const preferredKeys = [
    "summary",
    "description",
    "role",
    "belief",
    "status",
    "state",
    "detail",
    "value"
  ];
  for (const key of preferredKeys) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  const fallback = Object.values(value).find(
    (candidate): candidate is string => typeof candidate === "string" && Boolean(candidate.trim())
  );
  return fallback?.trim() ?? "已提交的正史条目";
}

export function displayEntityName(id: string, value: Record<string, unknown>): string {
  for (const key of ["name", "display_name", "title", "label"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return id.replace(/[-_]/g, " ");
}

export function eventMatches(
  event: HarnessEvent,
  loop: string,
  status: string,
  query: string
): boolean {
  if (loop !== "all" && event.loop_layer !== loop) return false;
  if (status !== "all" && event.status !== status) return false;
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [
    event.kind,
    event.atomic_action ?? "",
    event.message,
    event.routing_decision ?? "",
    event.artifact_path ?? ""
  ].some((value) => value.toLowerCase().includes(normalized));
}
