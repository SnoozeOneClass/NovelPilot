import { Send } from "lucide-react";
import styles from "./FeedbackComposer.module.css";

interface FeedbackComposerProps {
  value: string;
  sending: boolean;
  onChange: (value: string) => void;
  onSend: () => void;
}

export function FeedbackComposer({ value, sending, onChange, onSend }: FeedbackComposerProps) {
  function useSuggestion(suggestion: string) {
    onChange(value.trim() ? `${value.trimEnd()}；${suggestion}` : suggestion);
  }

  return (
    <footer className={styles.composer}>
      <textarea
        value={value}
        rows={1}
        disabled={sending}
        placeholder="告诉 NovelPilot 需要如何纠偏..."
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); onSend(); } }}
      />
      <span className={styles.scope}>注入时机：<strong>下一个安全检查点</strong></span>
      <div className={styles.suggestions}>{["节奏太快", "增加伏笔", "强化动机"].map((item) => <button key={item} onClick={() => useSuggestion(item)}>{item}</button>)}</div>
      <button className={styles.send} title="提交反馈" disabled={sending || !value.trim()} onClick={onSend}><Send size={17} /></button>
    </footer>
  );
}
