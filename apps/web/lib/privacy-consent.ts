export const PRIVACY_CONSENT_STORAGE_KEY = "hzcu-agent-privacy-consent";
export const PRIVACY_NOTICE_VERSION = "2026-08-28";

export type PrivacyConsentRecord = {
  version: string;
  acceptedAt: string;
};

export function parsePrivacyConsent(value: string | null): PrivacyConsentRecord | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PrivacyConsentRecord>;
    if (
      parsed.version !== PRIVACY_NOTICE_VERSION ||
      typeof parsed.acceptedAt !== "string" ||
      !Number.isFinite(Date.parse(parsed.acceptedAt))
    ) {
      return null;
    }
    return { version: parsed.version, acceptedAt: parsed.acceptedAt };
  } catch {
    return null;
  }
}

export function newPrivacyConsent(): PrivacyConsentRecord {
  return {
    version: PRIVACY_NOTICE_VERSION,
    acceptedAt: new Date().toISOString(),
  };
}
