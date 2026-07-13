import {
  CheckCircle2,
  CircleAlert,
  Database,
  FlaskConical,
  LockKeyhole,
  Settings2,
  Snowflake
} from "lucide-react";
import type { ExperimentFixtureStatus, OperationMode } from "../../types/domain";
import styles from "./ExperimentLab.module.css";

interface ExperimentLabProps {
  status: ExperimentFixtureStatus | null;
  operationMode: OperationMode;
  loading: boolean;
  busy: boolean;
  onFreeze: () => Promise<boolean>;
  onOpenSettings: () => void;
}

export function ExperimentLab({
  status,
  operationMode,
  loading,
  busy,
  onFreeze,
  onOpenSettings
}: ExperimentLabProps) {
  const checkpoint = status?.checkpoint ?? null;
  const existing = status?.existing_fixture ?? null;
  const usesParticipatoryMode = operationMode === "participatory";
  const canFreeze = usesParticipatoryMode && Boolean(status?.eligible) && !busy && !existing;

  async function freezeFixture() {
    if (!canFreeze) return;
    const confirmed = window.confirm(
      "将当前已提交检查点复制为不可变实验母本。该操作不会推进或修改当前小说，是否继续？"
    );
    if (confirmed) await onFreeze();
  }

  return (
    <section className={styles.lab}>
      <header className={styles.heading}>
        <div className={styles.titleIcon}><FlaskConical size={20} /></div>
        <div>
          <span>实验功能 · 独立于正常创作流程</span>
          <h1>实验室</h1>
          <p>制作可复现母本，后续由 None、Full 和消融配置从同一状态开始。</p>
        </div>
      </header>

      <article className={styles.fixtureCard}>
        <header>
          <div>
            <span><Snowflake size={15} /> 实验母本</span>
            <h2>冻结当前项目检查点</h2>
            <p>只复制已批准规划、已提交共享前文和正史；候选草稿与失败尝试不会进入母本。</p>
          </div>
          <span className={styles.scopeBadge}>EXPERIMENT ONLY</span>
        </header>

        <section className={styles.modeGuide} data-ready={usesParticipatoryMode}>
          <div className={styles.modeGuideHeading}>
            {usesParticipatoryMode ? <CheckCircle2 size={18} /> : <CircleAlert size={18} />}
            <div>
              <strong>制作母本必须使用参与模式</strong>
              <span>
                {usesParticipatoryMode
                  ? "当前项目已是参与模式。"
                  : "当前项目是全自动模式，请先切换后再生成实验母本。"}
              </span>
            </div>
            {!usesParticipatoryMode && (
              <button type="button" onClick={onOpenSettings}>
                <Settings2 size={15} />
                前往设置切换
              </button>
            )}
          </div>
          <ol>
            <li>在参与模式下完成并提交第一个故事弧。</li>
            <li>审阅并批准第二个故事弧规划。</li>
            <li>不要继续生成第二个故事弧的章节，立即回到实验室冻结母本。</li>
          </ol>
        </section>

        {loading && !status && <p className={styles.loading}>正在检查母本冻结条件...</p>}

        {status && !status.eligible && (
          <section className={styles.notReady}>
            <div><CircleAlert size={18} /><strong>检查点尚未就绪</strong></div>
            <p>实验室会保持可见，但只有满足以下条件后才能制作母本：</p>
            <ul>
              {status.issues.map((issue) => <li key={`${issue.code}-${issue.message}`}>{issue.message}</li>)}
            </ul>
          </section>
        )}

        {checkpoint && (
          <section className={styles.checkpoint}>
            <div className={styles.checkpointTitle}>
              <CheckCircle2 size={18} />
              <div><strong>可冻结检查点</strong><span>{checkpoint.source_title ?? checkpoint.source_project_name}</span></div>
            </div>
            <dl>
              <div><dt>当前故事弧</dt><dd>{checkpoint.active_arc_id}</dd></div>
              <div><dt>共享历史</dt><dd>{checkpoint.warmup_chapter_ids.length} 章</dd></div>
              <div><dt>实验区间</dt><dd>{checkpoint.target_chapter_count} 章</dd></div>
              <div><dt>检查点指纹</dt><dd title={checkpoint.checkpoint_fingerprint}>{checkpoint.checkpoint_fingerprint.slice(0, 12)}</dd></div>
            </dl>
          </section>
        )}

        {existing && (
          <section className={styles.existing}>
            <LockKeyhole size={18} />
            <div>
              <strong>这个检查点已经冻结</strong>
              <span>{existing.fixture_id}</span>
              <small>{existing.relative_path}</small>
            </div>
          </section>
        )}

        <footer>
          <div><Database size={15} /><span>母本保存在独立的 output/experiments/fixtures 目录。</span></div>
          <button
            disabled={!canFreeze}
            onClick={() => void freezeFixture()}
          >
            <Snowflake size={16} />
            {busy
              ? "正在冻结..."
              : existing
                ? "母本已冻结"
                : !usesParticipatoryMode
                  ? "请先切换参与模式"
                  : status?.eligible
                    ? "冻结为实验母本"
                    : "检查点未就绪"}
          </button>
        </footer>
      </article>
    </section>
  );
}
