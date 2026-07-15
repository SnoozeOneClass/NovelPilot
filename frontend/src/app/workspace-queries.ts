import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CanonKind } from "../features/workspace/workspace-utils";
import { canonFiles } from "../features/workspace/workspace-utils";

const emptyCanonContents: Record<CanonKind, string> = {
  characters: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  relationships: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  world_facts: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  foreshadowing: "{\"schema_version\":1,\"version\":1,\"items\":{}}"
};

export const workspaceQueryKeys = {
  project: (projectId: string) => ["workspace", projectId] as const,
  activeProject: (projectId: string) => ["workspace", projectId, "active-project"] as const,
  setup: (projectId: string) => ["workspace", projectId, "setup"] as const,
  bookRevision: (projectId: string) => ["workspace", projectId, "book-revision"] as const,
  readiness: (projectId: string) => ["workspace", projectId, "readiness"] as const,
  arc: (projectId: string) => ["workspace", projectId, "arc"] as const,
  experimentFixture: (projectId: string) => ["workspace", projectId, "experiment-fixture"] as const,
  profiles: () => ["profiles"] as const,
  artifactPaths: (projectId: string) => ["workspace", projectId, "artifact-paths"] as const,
  artifactSummaries: (projectId: string) => ["workspace", projectId, "artifact-summaries"] as const,
  completion: (projectId: string) => ["workspace", projectId, "completion"] as const,
  canonRoot: (projectId: string) => ["workspace", projectId, "canon"] as const,
  canon: (projectId: string, kind: CanonKind) => ["workspace", projectId, "canon", kind] as const,
  artifact: (projectId: string, path: string | null) => ["workspace", projectId, "artifact", path] as const
};

export function useWorkspaceQueries(projectId: string) {
  const activeProject = useQuery({ queryKey: workspaceQueryKeys.activeProject(projectId), queryFn: api.activeProject });
  const setup = useQuery({ queryKey: workspaceQueryKeys.setup(projectId), queryFn: api.setupState });
  const bookRevision = useQuery({
    queryKey: workspaceQueryKeys.bookRevision(projectId),
    queryFn: api.pendingBookRevision
  });
  const readiness = useQuery({ queryKey: workspaceQueryKeys.readiness(projectId), queryFn: api.readiness });
  const currentArc = useQuery({ queryKey: workspaceQueryKeys.arc(projectId), queryFn: api.currentArc });
  const experimentFixture = useQuery({
    queryKey: workspaceQueryKeys.experimentFixture(projectId),
    queryFn: api.experimentFixtureStatus
  });
  const profiles = useQuery({ queryKey: workspaceQueryKeys.profiles(), queryFn: api.profiles });
  const artifactPaths = useQuery({ queryKey: workspaceQueryKeys.artifactPaths(projectId), queryFn: api.listArtifacts });
  const artifactSummaries = useQuery({ queryKey: workspaceQueryKeys.artifactSummaries(projectId), queryFn: api.artifactSummaries });
  const completionAudit = useQuery({ queryKey: workspaceQueryKeys.completion(projectId), queryFn: api.completionAudit });
  const canonQueries = useQueries({
    queries: (Object.entries(canonFiles) as Array<[CanonKind, string]>).map(([kind, path]) => ({
      queryKey: workspaceQueryKeys.canon(projectId, kind),
      queryFn: async () => {
        try {
          return (await api.artifactContent(path)).content;
        } catch {
          return emptyCanonContents[kind];
        }
      }
    }))
  });

  const canonContents = Object.fromEntries(
    (Object.keys(canonFiles) as CanonKind[]).map((kind, index) => [kind, canonQueries[index]?.data ?? emptyCanonContents[kind]])
  ) as Record<CanonKind, string>;
  const error = [activeProject, setup, bookRevision, readiness, currentArc, experimentFixture, profiles, artifactPaths, artifactSummaries, completionAudit]
    .find((query) => query.error)?.error ?? null;

  return {
    activeProject,
    setup,
    bookRevision,
    readiness,
    currentArc,
    experimentFixture,
    profiles,
    artifactPaths,
    artifactSummaries,
    completionAudit,
    canonContents,
    error
  };
}
