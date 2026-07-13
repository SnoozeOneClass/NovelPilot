import { Moon, Sun } from "lucide-react";
import { useTheme } from "../../app/theme";
import styles from "./ThemeToggle.module.css";

export function ThemeToggle() {
  const { resolvedTheme, setPreference } = useTheme();
  const dark = resolvedTheme === "dark";
  return (
    <button
      type="button"
      className={styles.button}
      title={dark ? "切换到亮色主题" : "切换到暗色主题"}
      aria-label={dark ? "切换到亮色主题" : "切换到暗色主题"}
      onClick={() => setPreference(dark ? "light" : "dark")}
    >
      {dark ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}
