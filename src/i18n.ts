export type UiLanguage = 'en' | 'zh';
const storageKey = 'robot-log-ui-language';
let language: UiLanguage = (typeof localStorage !== 'undefined' && localStorage.getItem(storageKey) === 'zh') ? 'zh' : 'en';
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
