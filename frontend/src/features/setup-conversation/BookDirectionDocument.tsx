import { FileText, ListTree, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import type { SetupChangeSummary } from "./setup-planning";
import { parseDirectionDocument } from "./setup-planning";
import styles from "./BookDirectionDocument.module.css";

interface BookDirectionDocumentProps {
  markdown: string;
  revision: number;
  mode: "draft" | "candidate";
  changeSummary: SetupChangeSummary | null;
}

export function BookDirectionDocument({ markdown, revision, mode, changeSummary }: BookDirectionDocumentProps) {
  const sections = parseDirectionDocument(markdown);

  return (
    <section className={styles.documentWorkspace} aria-labelledby="book-direction-document-title">
      <header className={styles.documentHeader}>
        <div>
          <span><FileText size={14} />{mode === "candidate" ? "审阅候选" : "持续维护的计划"}</span>
          <h1 id="book-direction-document-title">Book Direction</h1>
        </div>
        <span className={styles.documentRevision}>{mode === "candidate" ? "候选" : "草稿"} r{revision}</span>
      </header>

      {changeSummary && (changeSummary.directionUpdated || changeSummary.ledgerDeltas.length > 0) && (
        <section className={styles.changeSummary} aria-label="本轮计划变更">
          <header><RefreshCw size={13} /><strong>本轮变更</strong></header>
          <p>{formatChangeSummary(changeSummary)}</p>
        </section>
      )}

      {sections.length > 1 && (
        <nav className={styles.documentOutline} aria-label="Book Direction 文档目录">
          <span><ListTree size={13} />目录</span>
          <div>
            {sections.map((section) => (
              <button
                key={section.anchor}
                type="button"
                data-level={section.level}
                onClick={() => document.getElementById(section.anchor)?.scrollIntoView({ behavior: "smooth", block: "start" })}
              >
                {section.title}
              </button>
            ))}
          </div>
        </nav>
      )}

      {sections.length === 0 ? (
        <div className={styles.documentPlaceholder}>
          <span><FileText size={26} /></span>
          <h2>方向会在讨论中逐步成形</h2>
          <p>先在“当前决策”中写下题材、人物、读者体验，或任何你不希望这本书成为的样子。这里不会强迫你填写固定模板。</p>
          <div aria-hidden="true"><i /><i /><i /></div>
        </div>
      ) : (
        <article className={styles.directionArticle}>
          {sections.map((section) => {
            const Heading = `h${section.level}` as "h1" | "h2" | "h3";
            return (
              <section key={section.anchor} id={section.anchor} className={styles.directionSection} data-level={section.level}>
                <Heading>{section.title}</Heading>
                <div>{renderBody(section.body)}</div>
              </section>
            );
          })}
        </article>
      )}
    </section>
  );
}

function renderBody(body: string): ReactNode {
  if (!body) return null;
  return body.split(/\n{2,}/).map((block, blockIndex) => {
    const lines = block.split("\n");
    const bullets = lines.map((line) => /^\s*[-*+]\s+(.+)$/.exec(line)?.[1]);
    if (bullets.every(Boolean)) {
      return <ul key={`list-${blockIndex}`}>{bullets.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}</ul>;
    }
    return <p key={`paragraph-${blockIndex}`}>{block}</p>;
  });
}

function formatChangeSummary(summary: SetupChangeSummary): string {
  const parts: string[] = [];
  if (summary.changedSections.length > 0) parts.push(`更新章节：${summary.changedSections.join("、")}`);
  else if (summary.directionUpdated) parts.push("Book Direction 已更新");
  if (summary.ledgerDeltas.length > 0) {
    parts.push(summary.ledgerDeltas.map(({ label, delta }) => `${label}${delta > 0 ? ` +${delta}` : ` ${delta}`}`).join("，"));
  }
  return parts.join("；");
}
