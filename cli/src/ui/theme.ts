/** Foreground-only bookish palette: terminal background remains user-owned. */
export function createNovelTheme(light = false) {
  const color = (hex: string) => (text: string) => {
    const n = Number.parseInt(hex, 16);
    return `\x1b[38;2;${n >> 16};${(n >> 8) & 255};${n & 255}m${text}\x1b[39m`;
  };
  return {
    gold: color(light ? 'A47818' : 'D8B264'),
    teal: color(light ? '367C7B' : '75B9AE'),
    green: color(light ? '477541' : '90BD87'),
    muted: color(light ? '81796B' : 'A59C8C'),
    line: color(light ? 'B7AD9B' : '625E55'),
    error: color(light ? 'AE4940' : 'DF8877'),
    bold: (text: string) => `\x1b[1m${text}\x1b[22m`,
    text: (text: string) => text,
  };
}
export const novelTheme = createNovelTheme();
