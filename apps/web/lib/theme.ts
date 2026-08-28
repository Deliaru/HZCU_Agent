export const THEME_STORAGE_KEY = "hzcu-agent-theme";

export const THEME_IDS = ["minimal", "character"] as const;

export type ThemeId = (typeof THEME_IDS)[number];

export function parseTheme(value: unknown): ThemeId | null {
  return value === "minimal" || value === "character" ? value : null;
}

export const THEME_INIT_SCRIPT = `(() => {
  try {
    const value = localStorage.getItem("${THEME_STORAGE_KEY}");
    document.documentElement.dataset.theme =
      value === "minimal" || value === "character" ? value : "unresolved";
  } catch {
    document.documentElement.dataset.theme = "unresolved";
  }
})();`;
