import { useEffect, useState } from "react";
import { api } from "./api/client";
import { ProjectSelector } from "./features/project-selector/ProjectSelector";
import { Workspace } from "./features/workspace/Workspace";
import type { ProjectSummary } from "./types/domain";

export function App() {
  const [activeProject, setActiveProject] = useState<ProjectSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .activeProject()
      .then(setActiveProject)
      .catch(() => setActiveProject(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="app-loading">Novelpilot</div>;
  }

  if (!activeProject) {
    return <ProjectSelector onProjectOpened={setActiveProject} />;
  }

  return <Workspace project={activeProject} onProjectClosed={() => setActiveProject(null)} />;
}

