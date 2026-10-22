import { Icon } from "./icons";
import {
  WEB_SEARCH_PRIVACY_NOTE,
  WEB_SEARCH_TOGGLE_LABEL,
  WEB_SEARCH_TOGGLE_TITLE,
} from "./webSearchState.ts";

/**
 * 输入框旁的「联网」开关（P7-S12）。只在运维已配好（ready）时渲染；默认每条消息都关，勾选只对下一条消息有效。
 * 禁用条件由调用方传入，与输入框一致。
 */
export function WebSearchToggle({
  ready,
  on,
  disabled,
  onChange,
}: {
  ready: boolean;
  on: boolean;
  disabled: boolean;
  onChange: (on: boolean) => void;
}) {
  if (!ready) return null;
  return (
    <button
      type="button"
      className="web-search-toggle"
      aria-pressed={on}
      disabled={disabled}
      title={WEB_SEARCH_TOGGLE_TITLE}
      onClick={() => onChange(!on)}
    >
      <Icon name="globe" />
      {WEB_SEARCH_TOGGLE_LABEL}
    </button>
  );
}

/** 勾选后的隐私提示：搜索词会离开本系统。纯文本。 */
export function WebSearchNote({ ready, on }: { ready: boolean; on: boolean }) {
  if (!ready || !on) return null;
  return (
    <p className="web-search-note" role="status">
      {WEB_SEARCH_PRIVACY_NOTE}
    </p>
  );
}
