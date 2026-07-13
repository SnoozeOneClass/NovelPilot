import { useVirtualizer } from "@tanstack/react-virtual";
import { Check, Circle, FileJson, FileText } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { formatArtifactTitle, formatGenericStatus } from "../../types/display";
import type { ArtifactSummary } from "../../types/domain";
import { chapterIdsFromArtifacts } from "../workspace/workspace-utils";
import styles from "./StoryWorldView.module.css";

interface ChapterBrowserProps {
  activeChapterId: string | null;
  artifactPaths: string[];
  summaries: ArtifactSummary[];
  onSelectArtifact: (path: string) => void;
}

const orderedFiles = ["context_snapshot.json", "goal.md", "draft.md", "observations.json", "review.md", "verification.json", "final.md"];

function chapterNumber(chapterId: string): string {
  const match = chapterId.match(/(\d+)$/);
  return match ? String(Number(match[1])).padStart(2, "0") : chapterId;
}

export function ChapterBrowser({ activeChapterId, artifactPaths, summaries, onSelectArtifact }: ChapterBrowserProps) {
  const chapterIds = useMemo(() => chapterIdsFromArtifacts(artifactPaths), [artifactPaths]);
  const [selectedChapterId, setSelectedChapterId] = useState(activeChapterId ?? chapterIds.at(-1) ?? "");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setSelectedChapterId(activeChapterId ?? chapterIds.at(-1) ?? "");
  }, [activeChapterId]);

  useEffect(() => {
    if (selectedChapterId && chapterIds.includes(selectedChapterId)) return;
    setSelectedChapterId(activeChapterId ?? chapterIds.at(-1) ?? "");
  }, [activeChapterId, chapterIds, selectedChapterId]);

  const virtualizer = useVirtualizer({
    count: chapterIds.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 58,
    overscan: 8,
    initialRect: { width: 280, height: 520 }
  });
  const virtualRows = virtualizer.getVirtualItems();
  const visibleRows = virtualRows.length ? virtualRows : Array.from({ length: Math.min(chapterIds.length, 12) }, (_, index) => ({ index, start: index * 58, size: 58 }));
  const chapterSummaries = summaries
    .filter((summary) => summary.path.startsWith(`chapters/${selectedChapterId}/`))
    .sort((left, right) => orderedFiles.indexOf(left.path.split("/").at(-1) ?? "") - orderedFiles.indexOf(right.path.split("/").at(-1) ?? ""));
  const committed = chapterSummaries.some((summary) => summary.kind === "final" && summary.committed);
  const verification = chapterSummaries.find((summary) => summary.kind === "verification") ?? null;

  return (
    <section className={styles.chapterBrowser}>
      <aside className={styles.chapterList}>
        <header><div><p>章节索引</p><strong>{chapterIds.length} 章</strong></div><small>按创作顺序</small></header>
        <div ref={scrollRef} className={styles.virtualViewport}>
          <div style={{ height: virtualizer.getTotalSize() || chapterIds.length * 58, position: "relative" }}>
            {visibleRows.map((item) => {
              const chapterId = chapterIds[item.index];
              const final = summaries.find((summary) => summary.path === `chapters/${chapterId}/final.md` && summary.committed);
              return (
                <button
                  key={chapterId}
                  className={selectedChapterId === chapterId ? styles.selected : ""}
                  style={{ position: "absolute", transform: `translateY(${item.start}px)`, height: item.size, width: "100%" }}
                  onClick={() => setSelectedChapterId(chapterId)}
                >
                  <span>{final ? <Check size={13} /> : <Circle size={12} />}</span>
                  <div><strong>第 {chapterNumber(chapterId)} 章</strong><small>{chapterId}</small></div>
                  <em>{chapterId === activeChapterId ? "当前" : final ? "已提交" : "候选"}</em>
                </button>
              );
            })}
          </div>
          {!chapterIds.length && <p className={styles.empty}>Harness 尚未生成章节产物。</p>}
        </div>
      </aside>

      <main className={styles.chapterDetail}>
        <header>
          <div><p>章节详情</p><h2>{selectedChapterId ? `第 ${chapterNumber(selectedChapterId)} 章` : "尚未选择章节"}</h2></div>
          {selectedChapterId && <span data-status={committed ? "passed" : verification?.status ?? "pending"}>{committed ? "已提交正史" : verification ? formatGenericStatus(verification.status) : "等待验证"}</span>}
        </header>
        {selectedChapterId && (
          <>
            <section className={styles.chapterSignals}>
              <div><span>候选边界</span><strong>{committed ? "final.md 已提交" : "草稿不计入正史"}</strong></div>
              <div><span>验证路由</span><strong>{verification?.routing_decision ?? "尚无路由建议"}</strong></div>
              <div><span>信号数量</span><strong>{verification?.signals.length ?? 0}</strong></div>
            </section>
            <section className={styles.chapterArtifacts}>
              <h3>章节契约与产物</h3>
              {chapterSummaries.map((summary) => {
                const isJson = summary.path.endsWith(".json");
                return (
                  <button key={summary.path} onClick={() => onSelectArtifact(summary.path)}>
                    <span>{isJson ? <FileJson size={16} /> : <FileText size={16} />}</span>
                    <div><strong>{formatArtifactTitle(summary)}</strong><small>{summary.path}</small></div>
                    <em data-status={summary.status}>{formatGenericStatus(summary.status)}</em>
                  </button>
                );
              })}
              {!chapterSummaries.length && <p className={styles.empty}>这个章节没有可读取的产物。</p>}
            </section>
          </>
        )}
      </main>
    </section>
  );
}
