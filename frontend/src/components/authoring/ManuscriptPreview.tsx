import { useQuery } from "@tanstack/react-query";
import { authoringApi } from "../../api/authoring-client";
import styles from "../../AuthoringApp.module.css";

export function ManuscriptPreview({ projectId }: { projectId: string }) {
  const manuscript = useQuery({
    queryKey: ["authoring", "manuscript", projectId, "txt"],
    queryFn: ({ signal }) => authoringApi.manuscript(projectId, signal),
    // A completed manuscript is immutable; progress/SSE refreshes must not replace it.
    staleTime: Infinity,
    retry: false
  });
  return <section className={`${styles.card} ${styles.previewCard}`} aria-labelledby="manuscript-title">
    <h2 id="manuscript-title">成稿预览</h2>
    <p className={styles.muted}>以下是下载 TXT 时的完整正文。</p>
    {manuscript.isFetching && <p role="status">正在读取成稿…</p>}
    {manuscript.error && <div role="alert" className={styles.error}>
      <p>{manuscript.error.message}</p><button className={styles.linkButton} disabled={manuscript.isFetching} onClick={() => { void manuscript.refetch(); }}>重新读取成稿</button>
    </div>}
    {manuscript.data && <pre className={styles.manuscript} tabIndex={0} aria-label="成稿正文">{manuscript.data.text}</pre>}
  </section>;
}
