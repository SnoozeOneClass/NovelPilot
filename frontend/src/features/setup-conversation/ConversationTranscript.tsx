import type { SetupMessage } from "../../types/domain";
import styles from "./ConversationTranscript.module.css";

interface ConversationTranscriptProps {
  messages: SetupMessage[];
  emptyText?: string;
}

export function ConversationTranscript({ messages, emptyText = "对话将在这里连续显示。" }: ConversationTranscriptProps) {
  if (messages.length === 0) return <p className={styles.empty}>{emptyText}</p>;

  return (
    <div className={styles.transcript} aria-label="全书共创完整对话">
      {messages.map((message) => (
        <article key={message.id} className={styles.message} data-role={message.role}>
          <header>
            <strong>{message.role === "user" ? "你" : "NovelPilot"}</strong>
            <span>第 {message.turn} 轮{message.model_snapshot ? ` · ${message.model_snapshot}` : ""}</span>
          </header>
          <p>{message.content}</p>
        </article>
      ))}
    </div>
  );
}
