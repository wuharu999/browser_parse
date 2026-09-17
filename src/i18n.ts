export type UiLanguage = 'en' | 'zh';
const storageKey = 'robot-log-ui-language';
function detectInitialLanguage(): UiLanguage {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem(storageKey);
    if (saved === 'zh' || saved === 'en') return saved;
  }
  if (typeof navigator !== 'undefined' && navigator.language && navigator.language.toLowerCase().startsWith('zh')) {
    return 'zh';
  }
  return 'en';
}

let language: UiLanguage = detectInitialLanguage();
const listeners = new Set<() => void>();
export const uiLanguage = (): UiLanguage => language;
export const t = (english: string, chinese: string): string => language === 'zh' ? chinese : english;
export function setUiLanguage(next: UiLanguage): void {
  language = next;
  if (typeof localStorage !== 'undefined') localStorage.setItem(storageKey, next);
  if (typeof document !== 'undefined') document.documentElement.lang = next;
  for (const listener of listeners) listener();
}
export function onUiLanguage(listener: () => void): void { listeners.add(listener); }
