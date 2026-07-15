import type { SetupStateDocument } from "../../types/domain";

export type SetupPlanningStage = "exploration" | "convergence" | "review" | "approved";

export interface DirectionSection {
  anchor: string;
  title: string;
  level: 1 | 2 | 3;
  body: string;
}

export interface SetupChangeSummary {
  changedSections: string[];
  ledgerDeltas: Array<{ label: string; delta: number }>;
  directionUpdated: boolean;
}

const ledgerFields: Array<{
  key: "confirmed_decisions" | "unresolved_questions" | "assumptions" | "contradictions" | "superseded_decisions";
  label: string;
}> = [
  { key: "confirmed_decisions", label: "已确认" },
  { key: "unresolved_questions", label: "待澄清" },
  { key: "assumptions", label: "假设" },
  { key: "contradictions", label: "矛盾" },
  { key: "superseded_decisions", label: "已取代" }
];

export function deriveSetupPlanningStage(state: SetupStateDocument): SetupPlanningStage {
  if (state.approved) return "approved";
  if (state.candidate) return "review";
  if (state.direction_draft.trim()) return "convergence";
  return "exploration";
}

export function parseDirectionDocument(markdown: string): DirectionSection[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const sections: DirectionSection[] = [];
  const anchorCounts = new Map<string, number>();
  let title = "";
  let level: 1 | 2 | 3 = 1;
  let body: string[] = [];

  const pushSection = () => {
    const content = body.join("\n").trim();
    if (!title && !content) return;
    const base = slugify(title || "开篇说明");
    const count = (anchorCounts.get(base) ?? 0) + 1;
    anchorCounts.set(base, count);
    sections.push({
      anchor: count === 1 ? `direction-${base}` : `direction-${base}-${count}`,
      title: title || "开篇说明",
      level,
      body: content
    });
  };

  for (const line of lines) {
    const match = /^(#{1,3})\s+(.+?)\s*$/.exec(line);
    if (!match) {
      body.push(line);
      continue;
    }
    pushSection();
    title = match[2].replace(/\s+#+\s*$/, "").trim();
    level = match[1].length as 1 | 2 | 3;
    body = [];
  }
  pushSection();
  return sections;
}

export function summarizeSetupChanges(
  previous: SetupStateDocument,
  next: SetupStateDocument
): SetupChangeSummary {
  const previousSections = indexSections(parseDirectionDocument(previous.direction_draft));
  const nextSections = indexSections(parseDirectionDocument(next.direction_draft));
  const changedSections: string[] = [];

  for (const [key, section] of nextSections) {
    if (previousSections.get(key)?.body !== section.body) changedSections.push(section.title);
  }
  for (const [key, section] of previousSections) {
    if (!nextSections.has(key) && !changedSections.includes(section.title)) changedSections.push(section.title);
  }

  const ledgerDeltas = ledgerFields.flatMap(({ key, label }) => {
    const delta = next[key].length - previous[key].length;
    return delta === 0 ? [] : [{ label, delta }];
  });

  return {
    changedSections,
    ledgerDeltas,
    directionUpdated: previous.direction_draft !== next.direction_draft
  };
}

function indexSections(sections: DirectionSection[]): Map<string, DirectionSection> {
  const occurrences = new Map<string, number>();
  return new Map(sections.map((section) => {
    const base = `${section.level}:${section.title.toLocaleLowerCase()}`;
    const occurrence = (occurrences.get(base) ?? 0) + 1;
    occurrences.set(base, occurrence);
    return [`${base}:${occurrence}`, section];
  }));
}

function slugify(value: string): string {
  const normalized = value
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "section";
}
