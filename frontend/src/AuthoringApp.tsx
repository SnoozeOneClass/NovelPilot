import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { authoringApi } from "./api/authoring-client";
import { AuthoringHome } from "./components/authoring/AuthoringHome";
import { AuthoringWorkspace } from "./components/authoring/AuthoringWorkspace";
import { ModelSettings } from "./components/authoring/ModelSettings";
import {
  decodeAuthoringEvent,
  type AuthoringProject,
  type ModelSettingsDocument
} from "./types/authoring";
import styles from "./AuthoringApp.module.css";

const projectsKey = ["authoring", "projects"] as const;
const settingsKey = ["authoring", "model-settings"] as const;
const projectKey = (id: string) => ["authoring", "project", id] as const;

function readSelection(): string | null {
  try { return localStorage.getItem("novelpilot.authoring.project-id"); } catch { return null; }
}

export function AuthoringApp() {
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState(readSelection);
  const [notice, setNotice] = useState<{ projectId: string; message: string } | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const projects = useQuery({ queryKey: projectsKey, queryFn: authoringApi.listProjects, refetchInterval: 3000 });
  const settings = useQuery({ queryKey: settingsKey, queryFn: authoringApi.modelSettings });
  const project = useQuery({
    queryKey: projectKey(selectedId ?? "none"),
    queryFn: () => authoringApi.getProject(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: 3000
  });

  useEffect(() => {
    if (!selectedId || !project.data) return;
    const source = new EventSource(authoringApi.eventStreamUrl(selectedId, project.data.latest_event_sequence));
    source.addEventListener("authoring_event", (raw) => {
      const event = raw instanceof MessageEvent ? decodeAuthoringEvent(String(raw.data)) : null;
      if (event === null || event.project_id !== selectedId) return;
      void client.invalidateQueries({ queryKey: projectKey(selectedId) });
      void client.invalidateQueries({ queryKey: projectsKey });
    });
    return () => source.close();
  }, [client, project.data?.latest_event_sequence, selectedId]);

  function select(id: string | null) {
    setSelectedId(id);
    setNotice(null);
    try {
      if (id) localStorage.setItem("novelpilot.authoring.project-id", id);
      else localStorage.removeItem("novelpilot.authoring.project-id");
    } catch { /* Selection remains valid for this tab. */ }
  }

  function changed(value: AuthoringProject) {
    client.setQueryData(projectKey(value.project_id), value);
    void client.invalidateQueries({ queryKey: projectsKey });
  }

  async function created(value: AuthoringProject) {
    changed(value);
    select(value.project_id);
    setStartingId(value.project_id);
    try { changed((await authoringApi.startOrResume(value.project_id)).project); }
    catch (error) {
      setNotice({ projectId: value.project_id, message: error instanceof Error ? error.message : "作品已创建，暂时无法启动，请继续创作重试" });
    } finally { setStartingId(null); }
  }

  function settingsChanged(document: ModelSettingsDocument) {
    client.setQueryData(settingsKey, document);
  }

  const settingsPage = settingsOpen && <ModelSettings document={settings.data} loading={settings.isLoading}
    error={settings.error} onRetry={() => { void settings.refetch(); }} onChanged={settingsChanged} onBack={() => setSettingsOpen(false)} />;

  if (!selectedId) return <>
    <div hidden={settingsOpen}><AuthoringHome
    projects={projects.data ?? []}
    settings={settings.data}
    error={projects.error ?? settings.error}
    onSelect={select}
    onCreated={created}
    onSettings={() => setSettingsOpen(true)}
  /></div>{settingsPage}</>;
  if (settingsOpen) return settingsPage;
  if (project.isLoading) return <main className={styles.center} role="status">正在读取创作进度…</main>;
  if (!project.data || project.error) return <main className={styles.center}>
    <CircleAlert /><p>{project.error instanceof Error ? project.error.message : "作品不存在"}</p>
    <button onClick={() => select(null)}>返回作品列表</button>
  </main>;
  return <AuthoringWorkspace key={selectedId} project={project.data} settings={settings.data}
    settingsError={settings.error} onSettings={() => setSettingsOpen(true)}
    starting={startingId === selectedId} notice={notice?.projectId === selectedId ? notice.message : null}
    onNotice={(message) => setNotice(message === null ? null : { projectId: selectedId, message })}
    onBack={() => select(null)} onChanged={changed}
    onRefresh={() => { void client.invalidateQueries({ queryKey: projectKey(selectedId) }); }} />;
}

