import { Badge } from "../../components/ui/Badge";
import { Separator } from "../../components/ui/Separator";
import type { SetupStateDocument } from "../../types/domain";
import styles from "./DirectionInspector.module.css";

export function DirectionLedger({ state }: { state: SetupStateDocument }) {
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
        <Badge>r{state.revision}</Badge>
      </header>
      <p className={styles.summary}>{state.discussion_summary || "讨论结论会持续汇总到这里。"}</p>
      <Separator />
      <div className={styles.ledger}>
        {ledger.map((section) => (
          <section key={section.title} data-tone={section.tone}>
            <header><h3>{section.title}</h3><Badge>{section.items.length}</Badge></header>
            <p>{section.description}</p>
            {section.items.length > 0
              ? <ul>{section.items.map((item) => <li key={item}>{item}</li>)}</ul>
              : <em>暂无记录</em>}
          </section>
        ))}
      </div>
    </section>
  );
}
