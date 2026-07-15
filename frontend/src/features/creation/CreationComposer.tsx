import { Send } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Textarea } from "../../components/ui/Textarea";
import styles from "./CreationView.module.css";

interface CreationComposerProps {
  value: string;
  sending: boolean;
  mode: "feedback" | "arc_revision";
  onChange: (value: string) => void;
  onSend: () => void;
}

export function CreationComposer({ value, sending, mode, onChange, onSend }: CreationComposerProps) {
  const placeholder = mode === "arc_revision"
    ? "说明希望调整的故事弧目标、冲突、节奏或章节方向……"
    : "告诉 NovelPilot 后续创作需要注意什么……";
  return (
    <footer className={styles.composer}>
      <Textarea
        rows={2}
        value={value}
        disabled={sending}
        placeholder={placeholder}
        aria-label={mode === "arc_revision" ? "故事弧修改意见" : "创作反馈"}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            onSend();
          }
        }}
      />
      <div className={styles.composerFooter}>
        <span>
          {mode === "arc_revision"
            ? "发送后会重新规划当前故事弧。"
            : "生成中也可以发送；系统会在本轮完成后纳入后续创作。"}
        </span>
        <Button variant="primary" disabled={sending || !value.trim()} onClick={onSend}>
          <Send size={15} />{sending ? "发送中…" : mode === "arc_revision" ? "发送修改意见" : "发送反馈"}
        </Button>
      </div>
    </footer>
  );
}
