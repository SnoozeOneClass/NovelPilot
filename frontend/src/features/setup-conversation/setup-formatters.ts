export function formatBookTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) return "";
  return trimmed.startsWith("《") && trimmed.endsWith("》") ? trimmed : `《${trimmed}》`;
}
