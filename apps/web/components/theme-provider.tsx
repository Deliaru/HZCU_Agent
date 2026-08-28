"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  parseTheme,
  THEME_STORAGE_KEY,
  type ThemeId,
} from "@/lib/theme";
import {
  newPrivacyConsent,
  parsePrivacyConsent,
  PRIVACY_CONSENT_STORAGE_KEY,
} from "@/lib/privacy-consent";

import { PrivacyNotice } from "./privacy-notice";
import { ThemePicker } from "./theme-picker";

type ThemeContextValue = {
  theme: ThemeId | null;
  setTheme: (theme: ThemeId) => void;
  showPrivacyNotice: () => void;
  withdrawPrivacyConsent: () => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId | null>(null);
  const [consentState, setConsentState] = useState<"loading" | "required" | "accepted">("loading");
  const [noticeOpen, setNoticeOpen] = useState(false);

  useEffect(() => {
    const rootTheme = parseTheme(document.documentElement.dataset.theme);
    let storedTheme: ThemeId | null = null;
    try {
      storedTheme = parseTheme(localStorage.getItem(THEME_STORAGE_KEY));
    } catch {
      // Storage can be unavailable in privacy-restricted browsers.
    }
    const initial = rootTheme ?? storedTheme;
    if (initial) {
      document.documentElement.dataset.theme = initial;
      setThemeState(initial);
    }
  }, []);

  useEffect(() => {
    try {
      setConsentState(
        parsePrivacyConsent(localStorage.getItem(PRIVACY_CONSENT_STORAGE_KEY))
          ? "accepted"
          : "required",
      );
    } catch {
      setConsentState("required");
    }
  }, []);

  const setTheme = useCallback((next: ThemeId) => {
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // The active document still changes theme when storage is unavailable.
    }
    setThemeState(next);
  }, []);

  const acceptPrivacyConsent = useCallback(() => {
    const consent = newPrivacyConsent();
    try {
      localStorage.setItem(PRIVACY_CONSENT_STORAGE_KEY, JSON.stringify(consent));
    } catch {
      // Acceptance remains valid for the current document if storage is unavailable.
    }
    setConsentState("accepted");
    setNoticeOpen(false);
  }, []);

  const showPrivacyNotice = useCallback(() => setNoticeOpen(true), []);

  const withdrawPrivacyConsent = useCallback(() => {
    try {
      localStorage.removeItem(PRIVACY_CONSENT_STORAGE_KEY);
    } catch {
      // The current document still returns to the required-consent gate.
    }
    setNoticeOpen(false);
    setConsentState("required");
  }, []);

  const value = useMemo(
    () => ({ theme, setTheme, showPrivacyNotice, withdrawPrivacyConsent }),
    [setTheme, showPrivacyNotice, theme, withdrawPrivacyConsent],
  );

  const accepted = consentState === "accepted";
  const appReady = accepted && theme !== null;

  return (
    <ThemeContext.Provider value={value}>
      {appReady ? <div className="theme-app-content">{children}</div> : null}
      {consentState === "required" || noticeOpen ? (
        <PrivacyNotice
          required={consentState === "required"}
          onAccept={acceptPrivacyConsent}
          onClose={() => setNoticeOpen(false)}
        />
      ) : null}
      {accepted && theme === null && !noticeOpen ? (
        <ThemePicker
          className="theme-first-visit"
          heading="选择你喜欢的界面"
          description="两种主题功能完全相同，之后也可以在“我的空间”里随时切换。"
          onSelect={setTheme}
        />
      ) : null}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside ThemeProvider");
  return context;
}
