import {
  CheckCircle2,
  CircleAlert,
  Database,
  FlaskConical,
  LockKeyhole,
  RotateCw,
  Snowflake
} from "lucide-react";
import type {
  BenchmarkFixtureLifecycle,
  ExperimentFixtureStatus,
  ProjectKind
} from "../../types/domain";
import styles from "./ExperimentLab.module.css";

interface ExperimentLabProps {
  status: ExperimentFixtureStatus | null;
  projectKind: ProjectKind;
  lifecycle: BenchmarkFixtureLifecycle | null;
  loading: boolean;
  busy: boolean;
  onRetry: () => Promise<boolean>;
}

export function ExperimentLab({
  status,
  projectKind,
  lifecycle,
  loading,
  busy,
  onRetry
}: ExperimentLabProps) {
  const checkpoint = status?.checkpoint ?? null;
  const existing = status?.existing_fixture ?? null;
  const fixtureLifecycle = status?.lifecycle ?? lifecycle;
  const declaredMother = projectKind === "benchmark_mother";
  const retryable = declaredMother
    && Boolean(status?.eligible)
    && fixtureLifecycle?.status !== "frozen"
    && !busy;

  return (
    <section className={styles.lab}>
      <header className={styles.heading}>
        <div className={styles.titleIcon}><FlaskConical size={20} /></div>
        <div>
          <span>实验功能 · 独立于正常创作流程</span>
          <h1>实验室</h1>
          <p>查看不可变母本状态；后续 None、Full 和消融实验从同一检查点开始。</p>
        </div>
      </header>

      <article className={styles.fixtureCard}>
        <header>
          <div>
            <span><Snowflake size={15} /> 实验母本</span>
            <h2>实验母本冻结</h2>
            <p>母本项目在第二故事弧审批后自动冻结；实验室只负责状态、完整性与失败重试。</p>
          </div>
          <span className={styles.scopeBadge}>仅限实验</span>
        </header>

        {!declaredMother ? (
          <section className={styles.notReady}>
            <div><CircleAlert size={18} /><strong>当前小说不是实验母本项目</strong></div>
            <p>母本身份只能在新建小说时选择，普通小说不会在这里被冻结或转换。</p>
          </section>
        ) : (
          <>
            <section className={styles.modeGuide} data-ready="true">
              <div className={styles.modeGuideHeading}>
                <CheckCircle2 size={18} />
                <div>
                  <strong>母本制作使用正常参与模式流程</strong>
                  <span>冻结前仍可正常讨论、反馈和修订；创作模式保持锁定。</span>
                </div>
              </div>
              <ol>
                <li>正常完成并提交第一故事弧。</li>
                <li>正常生成并审阅第二故事弧规划。</li>
                <li>批准后后端自动冻结母本并永久停止源项目续写。</li>
              </ol>
            </section>

            {loading && !status && <p className={styles.loading}>正在读取母本状态...</p>}

            {status && !status.eligible && !existing && (
              <section className={styles.notReady}>
                <div><CircleAlert size={18} /><strong>母本检查点尚未就绪</strong></div>
                <ul>
                  {status.issues.map((issue) => (
                    <li key={`${issue.code}-${issue.message}`}>{issue.message}</li>
                  ))}
                </ul>
              </section>
            )}

            {fixtureLifecycle?.status === "freeze_failed" && (
              <section className={styles.notReady}>
                <div><CircleAlert size={18} /><strong>自动冻结未完成</strong></div>
                <p>{fixtureLifecycle.failure_message ?? "本地母本发布失败，可以安全重试。"}</p>
                <small>重试只重新发布本地母本，不会调用模型、重新审批或改写小说内容。</small>
              </section>
            )}

            {checkpoint && (
              <section className={styles.checkpoint}>
                <div className={styles.checkpointTitle}>
                  <CheckCircle2 size={18} />
                  <div><strong>固定检查点</strong><span>{checkpoint.source_title ?? checkpoint.source_project_name}</span></div>
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
                  <strong>
                    {fixtureLifecycle?.status === "frozen"
                      ? "母本已经冻结，源项目保持只读"
                      : "检测到已发布母本，等待完成状态同步"}
                  </strong>
                  <span>{existing.fixture_id}</span>
                  <small>{existing.fixture_version} · {existing.integrity_verified ? "完整性已校验" : "完整性待校验"}</small>
                  <small>{existing.relative_path}</small>
                </div>
              </section>
            )}
          </>
        )}

        <footer>
          <div><Database size={15} /><span>母本保存在独立的 output/experiments/fixtures 目录。</span></div>
          {declaredMother && (
            <button disabled={!retryable} onClick={() => void onRetry()}>
              {fixtureLifecycle?.status === "freeze_failed" ? <RotateCw size={16} /> : <Snowflake size={16} />}
              {busy
                ? "正在重试..."
                : fixtureLifecycle?.status === "frozen"
                  ? "母本已冻结"
                  : existing && retryable
                    ? "完成母本状态同步"
                  : retryable
                    ? "重试母本冻结"
                    : "等待自动冻结"}
            </button>
          )}
        </footer>
      </article>
    </section>
  );
}
