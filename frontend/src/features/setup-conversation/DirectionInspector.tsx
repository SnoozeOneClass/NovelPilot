import type { BookDirectionConstraints, SetupStateDocument } from "../../types/domain";
import styles from "./SetupConversation.module.css";

const constraintSections: Array<{ key: keyof BookDirectionConstraints; title: string }> = [
  { key: "confirmed", title: "已确认决定" },
  { key: "must_preserve", title: "必须维护" },
  { key: "must_avoid", title: "必须避免" },
  { key: "creative_freedoms", title: "创作自由" },
  { key: "open_decisions", title: "仍待决定" }
];

export function DirectionLedger({ state }: { state: SetupStateDocument }) {
  const candidate = state.candidate;
  const ledger = [
    { title: "已确认", description: "模型必须持续保留的用户决定", items: state.confirmed_decisions, tone: "confirmed" },
    { title: "待澄清", description: "尚未成为事实的问题", items: state.unresolved_questions, tone: "open" },
    { title: "当前假设", description: "可用于推演、但仍可被推翻", items: state.assumptions, tone: "assumption" },
    { title: "矛盾", description: "必须显式解决的冲突", items: state.contradictions, tone: "conflict" },
    {
      title: "已取代",
      description: "由用户明确撤销或替换的旧决定",
      items: state.superseded_decisions.map((item) => item.replacement ? `${item.decision} → ${item.replacement}` : `${item.decision} → 已撤销`),
      tone: "superseded"
    }
  ];

  return (
    <section className={styles.ledgerView} aria-labelledby="direction-ledger-title">
      <header>
        <div><span>Decision Ledger</span><h2 id="direction-ledger-title">方向账本</h2></div>
        <strong>r{state.revision}</strong>
      </header>
      <div className={styles.ledger}>
        {ledger.map((section) => (
          <section key={section.title} data-tone={section.tone}>
            <header><h3>{section.title}</h3><span>{section.items.length}</span></header>
            <p>{section.description}</p>
            {section.items.length > 0 ? <ul>{section.items.map((item) => <li key={item}>{item}</li>)}</ul> : <em>暂无记录</em>}
          </section>
        ))}
      </div>
      {candidate && (
        <section className={styles.contract}>
          <header><span>Review Contract</span><h2>滚动故事弧契约</h2></header>
          <pre>{candidate.rolling_plan_markdown}</pre>
          {constraintSections.map((section) => candidate.constraints[section.key].length > 0 ? (
            <div key={section.key}>
              <strong>{section.title}</strong>
              <ul>{candidate.constraints[section.key].map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          ) : null)}
        </section>
      )}
    </section>
  );
}
