import { Bot, Check, MonitorCog, Moon, Settings2, Sun } from "lucide-react";
import { useState } from "react";
import { api, formatApiError } from "../../api/client";
import { useTheme } from "../../app/theme";
import type { SettingsSection, ThemePreference } from "../../app/types";
import { formatOperationMode } from "../../types/display";
import type { LlmProfilesDocument, OperationMode, ProjectSummary } from "../../types/domain";
import { LlmProfilesPanel } from "../llm-profiles/LlmProfilesPanel";
import { AgentPolicyPanel } from "./AgentPolicyPanel";
import styles from "./SettingsView.module.css";

interface SettingsViewProps {
  project: ProjectSummary;
  sourceReadOnly?: boolean;
  onProjectChanged: (project: ProjectSummary) => void;
  onProfilesChanged: (profiles: LlmProfilesDocument) => void;
}

const sections: Array<{ id: SettingsSection; label: string; icon: typeof Settings2 }> = [
  { id: "project", label: "项目运行", icon: Settings2 },
  { id: "models", label: "LLM Profile", icon: Bot },
  { id: "appearance", label: "界面主题", icon: MonitorCog }
];

const themes: Array<{ id: ThemePreference; label: string; detail: string; icon: typeof Sun }> = [
  { id: "system", label: "跟随系统", detail: "自动匹配操作系统外观", icon: MonitorCog },
  { id: "light", label: "亮色", detail: "适合明亮环境", icon: Sun },
  { id: "dark", label: "暗色", detail: "适合长时间专注", icon: Moon }
];

export function SettingsView({ project, sourceReadOnly = false, onProjectChanged, onProfilesChanged }: SettingsViewProps) {
  const [section, setSection] = useState<SettingsSection>("project");
  const [profiles, setProfiles] = useState<LlmProfilesDocument | null>(null);
  const [savingMode, setSavingMode] = useState(false);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const { preference, resolvedTheme, setPreference } = useTheme();
  const motherModeLocked = project.metadata.project_kind === "benchmark_mother";
  const terminalSourceReadOnly = sourceReadOnly || motherModeLocked && (
    project.metadata.benchmark_fixture?.status === "frozen"
    || project.metadata.benchmark_fixture?.status === "freeze_failed"
  );
  const runLocked = project.metadata.run_status === "running"
    || project.metadata.run_status === "pause_requested"
    || project.metadata.run_status === "waiting_for_provider";
  const modeLocked = runLocked || motherModeLocked;

  async function changeMode(mode: OperationMode) {
    if (mode === project.metadata.operation_mode || savingMode || modeLocked) return;
    setSavingMode(true);
    setNotice(null);
    try {
      const nextProject = await api.updateProjectMode(mode);
      onProjectChanged(nextProject);
      setNotice({ kind: "success", text: `运行模式已切换为${formatOperationMode(mode)}。` });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSavingMode(false);
    }
  }

  return (
    <section className={styles.settings}>
      <aside className={styles.sectionNav}>
        <header><p>设置</p><h1>本地工作区</h1></header>
        <nav aria-label="设置分类">
          {sections.map((item) => {
            const Icon = item.icon;
            return <button key={item.id} className={section === item.id ? styles.active : ""} onClick={() => setSection(item.id)}><Icon size={16} /><span>{item.label}</span></button>;
          })}
        </nav>
        <footer><span>当前项目</span><strong>{project.title ?? project.name}</strong><small>{project.path}</small></footer>
      </aside>

      <main className={styles.settingsContent}>
        {section === "project" && (
          <section className={styles.preferencePage}>
            <header><p>项目运行</p><h2>参与方式</h2><span>控制故事弧计划是否需要人工确认。随时反馈在两种模式下都可用。</span></header>
            {notice && <p className={notice.kind === "error" ? styles.error : styles.success}>{notice.text}</p>}
            {motherModeLocked
              ? <p className={styles.warning}>实验母本项目永久使用参与模式，不能切换创作模式。</p>
              : modeLocked && <p className={styles.warning}>Harness 正在运行，模式将在当前动作结束并停止后才能修改。</p>}
            <div className={styles.modeOptions} role="radiogroup" aria-label="项目运行模式">
              {([
                { id: "full_auto" as const, title: "全自动模式", detail: "故事弧计划无需人工批准，Harness 在安全检查点自动继续。" },
                { id: "participatory" as const, title: "参与模式", detail: "每个当前故事弧计划必须由你批准，章节 Loop 仍由 Harness 连续推进。" }
              ]).map((mode) => {
                const active = project.metadata.operation_mode === mode.id;
                return <button key={mode.id} role="radio" aria-checked={active} disabled={savingMode || modeLocked} className={active ? styles.selected : ""} onClick={() => void changeMode(mode.id)}><span>{active ? <Check size={15} /> : null}</span><div><strong>{mode.title}</strong><p>{mode.detail}</p></div></button>;
              })}
            </div>
            <section className={styles.safetyNote}><strong>安全门禁保持不变</strong><p>正在运行时不能切换模式；参与模式下，未批准的故事弧不会进入章节创作。后端仍会校验所有状态转换。</p></section>
          </section>
        )}

        {section === "models" && (
          <div className={styles.modelsPage}>
            {terminalSourceReadOnly && <p className={styles.warning}>母本源项目已经停止创作，模型与 Agent 配置保持只读。</p>}
            <LlmProfilesPanel
              locked={terminalSourceReadOnly}
              onProfilesChanged={(nextProfiles) => {
                setProfiles(nextProfiles);
                onProfilesChanged(nextProfiles);
              }}
            />
            <AgentPolicyPanel
              project={project}
              profiles={profiles}
              locked={runLocked || terminalSourceReadOnly}
              onProjectChanged={onProjectChanged}
            />
          </div>
        )}

        {section === "appearance" && (
          <section className={styles.preferencePage}>
            <header><p>界面主题</p><h2>工作台外观</h2><span>亮色与暗色共享同一套语义色彩和组件布局。</span></header>
            <div className={styles.themeOptions} role="radiogroup" aria-label="界面主题">
              {themes.map((theme) => {
                const Icon = theme.icon;
                const active = preference === theme.id;
                return <button key={theme.id} role="radio" aria-checked={active} className={active ? styles.selected : ""} onClick={() => setPreference(theme.id)}><Icon size={18} /><div><strong>{theme.label}</strong><small>{theme.detail}</small></div>{active && <Check size={15} />}</button>;
              })}
            </div>
            <section className={styles.themePreview}><span>当前实际主题</span><strong>{resolvedTheme === "dark" ? "暗色" : "亮色"}</strong></section>
          </section>
        )}
      </main>
    </section>
  );
}
