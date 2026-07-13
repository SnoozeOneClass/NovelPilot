import { X } from "lucide-react";
import type { BookDirectionConstraints, SetupStateDocument } from "../../types/domain";
import styles from "./SetupConversation.module.css";

const constraintSections: Array<{ key: keyof BookDirectionConstraints; title: string }> = [
  { key: "confirmed", title: "已确认决定" },
  { key: "must_preserve", title: "必须维护" },
  { key: "must_avoid", title: "必须避免" },
  { key: "creative_freedoms", title: "创作自由" },
  { key: "open_decisions", title: "仍待决定" }
];

export function DirectionInspector({ state, open, onClose }: { state: SetupStateDocument; open: boolean; onClose: () => void }) {
  const candidate = state.candidate;
  const ledger = [
    { title: "已确认", items: state.confirmed_decisions, tone: "confirmed" },
    { title: "待澄清", items: state.unresolved_questions, tone: "open" },
    { title: "当前假设", items: state.assumptions, tone: "assumption" },
    { title: "矛盾", items: state.contradictions, tone: "conflict" },
    { title: "已取代", items: state.superseded_decisions.slice(-5).map((item) => item.replacement ? `${item.decision} → ${item.replacement}` : `${item.decision} → 已撤销`), tone: "superseded" }
  ];

  return (
    <aside className={styles.inspector} data-open={open}>
      <header><div><p>方向账本</p><h2>{state.approved ? "已批准方向" : `候选修订 r${state.revision}`}</h2></div><span>{state.direction_draft ? "已同步" : "待开始"}</span><button className={styles.inspectorClose} title="关闭方向账本" onClick={onClose}><X size={17} /></button></header>
      <section className={styles.directionDraft}>{state.direction_draft || "对话开始后，模型会在这里持续维护完整的全书方向草稿。"}</section>
      <div className={styles.ledger}>
        {ledger.map((section) => (
          <section key={section.title} data-tone={section.tone}>
            <h3>{section.title}<span>{section.items.length}</span></h3>
            {section.items.length ? <ul>{section.items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无</p>}
          </section>
        ))}
      </div>
      {candidate && (
        <section className={styles.contract}>
          <h3>滚动故事弧契约</h3>
          <pre>{candidate.rolling_plan_markdown}</pre>
          {constraintSections.map((section) => candidate.constraints[section.key].length ? (
            <div key={section.key}><strong>{section.title}</strong><ul>{candidate.constraints[section.key].map((item) => <li key={item}>{item}</li>)}</ul></div>
          ) : null)}
        </section>
      )}
    </aside>
  );
}
