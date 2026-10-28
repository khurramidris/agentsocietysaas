// Must be imported before "@upstream/i18n": the upstream webviews read the UI language
// from window.__AS_LANG__ when their i18n module initialises (Chinese otherwise).
declare global {
  interface Window {
    __AS_LANG__?: string;
  }
}

const LANG_KEY = "societas.lang";

function stored(): string | null {
  try {
    return localStorage.getItem(LANG_KEY);
  } catch {
    return null;
  }
}

window.__AS_LANG__ = stored() === "zh-CN" ? "zh-CN" : "en-US";

export function setUpstreamLanguage(lang: "en-US" | "zh-CN"): void {
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    /* private mode */
  }
}
