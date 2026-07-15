import { BookOpenText, GitBranch, ScrollText } from "lucide-react";
import { useState } from "react";
import type { StoryWorldTab } from "../../app/types";
import type { ArtifactSummary, CurrentArcState } from "../../types/domain";
import { CanonView } from "../workspace/CanonView";
import { StoryArcsView } from "../workspace/StoryArcsView";
import type { CanonKind } from "../workspace/workspace-utils";
import { ChapterBrowser } from "./ChapterBrowser";
import styles from "./StoryWorldView.module.css";

interface StoryWorldViewProps {
  currentArc: CurrentArcState | null;
  activeChapterId: string | null;
  artifactPaths: string[];
  summaries: ArtifactSummary[];
  canonContents: Record<CanonKind, string>;
  onSelectArtifact: (path: string) => void;
  onRefresh: () => Promise<void>;
}

const tabs: Array<{ id: StoryWorldTab; label: string; icon: typeof GitBranch }> = [
  { id: "arcs", label: "故事弧", icon: GitBranch },
  { id: "chapters", label: "章节", icon: BookOpenText },
  { id: "canon", label: "正史", icon: ScrollText }
];

export function StoryWorldView({ currentArc, activeChapterId, artifactPaths, summaries, canonContents, onSelectArtifact, onRefresh }: StoryWorldViewProps) {
  const [tab, setTab] = useState<StoryWorldTab>("arcs");

  return (
    <section className={styles.domain}>
      <header className={styles.domainHeader}>
        <div>
          <p>故事世界</p>
          <h1>滚动规划与已提交状态</h1>
        </div>
        <nav aria-label="故事世界视图">
          {tabs.map((item) => {
            const Icon = item.icon;
            return <button key={item.id} className={tab === item.id ? styles.active : ""} onClick={() => setTab(item.id)}><Icon size={15} />{item.label}</button>;
          })}
        </nav>
      </header>

      <div className={styles.content}>
        {tab === "arcs" && (
          <StoryArcsView
            currentArc={currentArc}
            activeChapterId={activeChapterId}
            artifactPaths={artifactPaths}
            summaries={summaries}
            onSelectArtifact={onSelectArtifact}
          />
        )}
        {tab === "chapters" && <ChapterBrowser activeChapterId={activeChapterId} artifactPaths={artifactPaths} summaries={summaries} onSelectArtifact={onSelectArtifact} />}
        {tab === "canon" && <CanonView contents={canonContents} summaries={summaries} onSelectArtifact={onSelectArtifact} onRefresh={onRefresh} />}
      </div>
    </section>
  );
}
