// OS で変わる表示（ショートカットの修飾キー。言語では変わらない。画面仕様 0.2、10.4）。
// 記号の文字はデータ（言語の文言ではない）。

const MAC = /Mac|iPhone|iPad/i;

export function isMac(): boolean {
  const nav = typeof navigator === "undefined" ? undefined : navigator;
  const platform = (nav as { userAgentData?: { platform?: string } } | undefined)?.userAgentData?.platform;
  return MAC.test(platform ?? nav?.platform ?? "");
}

/** {mod} に入れる文字: Windows と Linux は Ctrl、macOS は ⌘。 */
export function modifierKey(): string {
  return isMac() ? "⌘" : "Ctrl";
}

/** ショートカットの修飾キーが押されているか（macOS は ⌘、ほかは Ctrl）。 */
export function hasModifier(event: { ctrlKey: boolean; metaKey: boolean }): boolean {
  return isMac() ? event.metaKey : event.ctrlKey;
}
