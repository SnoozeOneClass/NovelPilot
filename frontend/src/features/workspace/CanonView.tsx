import { ChevronRight, Code2, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { ArtifactSummary } from "../../types/domain";
import {
  canonFiles,
  type CanonKind,
  displayEntityName,
  firstTextValue,
  parseCanonDocument
} from "./workspace-utils";

interface CanonViewProps {
  contents: Record<CanonKind, string>;
  summaries: ArtifactSummary[];
  onSelectArtifact: (path: string) => void;
  onRefresh: () => Promise<void>;
}

const tabs: Array<{ id: CanonKind; label: string; singular: string }> = [
  { id: "characters", label: "角色", singular: "角色" },
  { id: "relationships", label: "关系", singular: "关系" },
  { id: "world_facts", label: "世界事实", singular: "世界事实" },
  { id: "foreshadowing", label: "伏笔", singular: "伏笔" }
];

function initials(value: string): string {
  const compact = value.replace(/\s+/g, "");
  return compact.slice(0, 1).toUpperCase() || "正";
}

function itemTags(value: Record<string, unknown>): string[] {
  const tags: string[] = [];
  for (const key of ["role", "type", "species", "status", "category", "faction"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) tags.push(candidate.trim());
  }
  return tags.slice(0, 3);
}

export function CanonView({ contents, summaries, onSelectArtifact, onRefresh }: CanonViewProps) {
  const [activeKind, setActiveKind] = useState<CanonKind>("characters");
  const [query, setQuery] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const documents = useMemo(
    () => Object.fromEntries(
      tabs.map((tab) => [tab.id, parseCanonDocument(contents[tab.id])])
    ) as Record<CanonKind, ReturnType<typeof parseCanonDocument>>,
    [contents]
  );
  const activeDocument = documents[activeKind];
  const activeTab = tabs.find((tab) => tab.id === activeKind) ?? tabs[0];
  const entries = Object.entries(activeDocument.items).filter(([id, value]) => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return true;
    return `${id} ${displayEntityName(id, value)} ${firstTextValue(value)}`.toLowerCase().includes(normalized);
  });
  const committedPatches = summaries.filter((summary) => summary.kind === "committed_state_patch");
  const recentCommits = committedPatches.length === 0
    ? []
    : [
        ...committedPatches.slice(-3).reverse(),
        ...summaries.filter((summary) => summary.path.startsWith("canon/")).slice(0, 2)
      ];

  async function refresh() {
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <section className="np-surface canon-view">
      <header className="view-heading">
        <div>
          <h1>正史状态 · Canon</h1>
          <p>这里只展示已经通过验证并提交的状态，不展示候选草稿。</p>
        </div>
        <button className="icon-button" title="刷新正史状态" disabled={refreshing} onClick={() => void refresh()}>
          <RefreshCw size={17} />
        </button>
      </header>

      <nav className="tab-bar canon-tabs">
        {tabs.map((tab) => (
          <button key={tab.id} className={activeKind === tab.id ? "active" : ""} onClick={() => setActiveKind(tab.id)}>
            {tab.label}
            <span>{Object.keys(documents[tab.id].items).length}</span>
          </button>
        ))}
      </nav>

      <div className="canon-toolbar">
        <label>
          <Search size={17} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${activeTab.label}...`} />
        </label>
        <button className="outline-button" onClick={() => setShowRaw((value) => !value)}>
          <Code2 size={16} /> {showRaw ? "收起原始 JSON" : "查看原始 JSON"}
        </button>
      </div>

      <div className="canon-content-grid">
        <section className="canon-items">
          {entries.map(([id, value]) => {
            const name = displayEntityName(id, value);
            return (
              <article key={id}>
                <div className="entity-avatar">{initials(name)}</div>
                <div className="entity-copy">
                  <h2>{name}</h2>
                  <p>{firstTextValue(value)}</p>
                  <div className="entity-tags">
                    {itemTags(value).map((tag) => <span key={tag}>{tag}</span>)}
                    <span>v{activeDocument.version}</span>
                  </div>
                </div>
                <button className="text-link" onClick={() => setShowRaw(true)}>
                  查看详情 <ChevronRight size={15} />
                </button>
              </article>
            );
          })}
          {entries.length === 0 && (
            <div className="empty-state canon-empty">
              <div className="entity-avatar">{initials(activeTab.singular)}</div>
              <h2>还没有已提交的{activeTab.singular}</h2>
              <p>章节通过验证并提交状态补丁后，相关条目会出现在这里。</p>
            </div>
          )}
          <footer className="canon-source-line">
            <button className="text-button" onClick={() => onSelectArtifact(canonFiles[activeKind])}>查看数据文件</button>
            <span>数据来源：{canonFiles[activeKind]} · version {activeDocument.version}</span>
          </footer>
        </section>

        <aside className="canon-summary-panel">
          <h2>提交摘要</h2>
          <div className="canon-count-grid large">
            {tabs.map((tab) => (
              <button key={tab.id} onClick={() => setActiveKind(tab.id)}>
                <span>{tab.label}</span>
                <strong>{Object.keys(documents[tab.id].items).length}</strong>
              </button>
            ))}
          </div>
          <section className="recent-commits">
            <h3>最近提交</h3>
            <ul>
              {recentCommits.map((summary) => (
                <li key={summary.path}>
                  <span className="status-dot success" />
                  <button onClick={() => onSelectArtifact(summary.path)}>{summary.path}</button>
                  <small>{summary.detail}</small>
                </li>
              ))}
              {recentCommits.length === 0 && <li className="empty-line">暂无状态提交</li>}
            </ul>
          </section>
        </aside>
      </div>

      {showRaw && (
        <section className="raw-json-panel">
          <header>
            <strong>{canonFiles[activeKind]}</strong>
            <button className="text-button" onClick={() => setShowRaw(false)}>关闭</button>
          </header>
          <pre>{contents[activeKind]}</pre>
        </section>
      )}
    </section>
  );
}
