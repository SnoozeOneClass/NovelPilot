import { useState } from "react";
import { ProjectSelector } from "./features/project-selector/ProjectSelector";
import { Workspace } from "./features/workspace/Workspace";
import type { ProjectSummary } from "./types/domain";

export function App() {
  const [activeProject, setActiveProject] = useState<ProjectSummary | null>(null);

  if (!activeProject) {
    return <ProjectSelector onProjectOpened={setActiveProject} />;
  }

  return <Workspace project={activeProject} onProjectClosed={() => setActiveProject(null)} />;
}

