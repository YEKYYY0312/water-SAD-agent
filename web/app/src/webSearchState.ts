import { credentialRows, sourceLabel, WEB_SEARCH_KEY_SLOT, type CredentialRow } from "./settingsState.ts";
import type { WebSearchKeyStatus, WebSearchStatusView } from "./webSearchApi.ts";

// ---------------------------------------------------------------------------
// 输入框旁的「联网」开关：逐条消息勾选、默认关、不持久化
// ---------------------------------------------------------------------------

export const WEB_SEARCH_TOGGLE_LABEL = "联网";
/** 鼠标悬停说明（关着的时候也看得到）：只管本条消息，并带着隐私提醒。 */
export const WEB_SEARCH_TOGGLE_TITLE =
  "只对本条消息联网搜索，发送后自动关闭；搜索词会发往运维配置的搜索服务，请勿在问题里放保密信息";
/** 勾选后显示的隐私提示：搜索词会离开本系统发往运维配置的搜索服务。 */
export const WEB_SEARCH_PRIVACY_NOTE =
  "本条消息的搜索词会发往运维配置的搜索服务，请勿在问题里放保密信息；发送后自动关闭联网。";

/**
 * 开关是否渲染只看后端报告的 `ready`，且严格要求 `=== true`：旧后端没有这个接口、返回格式不对、字段缺失，
 * 一律按未就绪处理（开关不出现），不抛错、不打断主流程。
 */
export function isWebSearchReady(view: unknown): boolean {
  return typeof view === "object" && view !== null && (view as { ready?: unknown }).ready === true;
}

/**
 * 勾选属于它被勾选时的那个会话：会话变了（切会话 / 切项目 / 新建）当场回到关，切回来也不会复活；
 * 发送后回到关；发送没被受理时（例如 409）只在仍处于同一个会话时放回去，与草稿、附件的回滚一致；
 * 运维那边变成不可用（就绪状态翻成 false）时也回到关，之后恢复就绪不会自己再打开。
 * 每个事件都带会话 id，状态机不依赖闭包里的"当前会话"。
 */
export type WebSearchToggleState = { threadId: string | null; on: boolean };

export const NO_WEB_SEARCH_TOGGLE: WebSearchToggleState = { threadId: null, on: false };

export type WebSearchToggleEvent =
  | { type: "thread"; threadId: string | null }
  | { type: "set"; threadId: string | null; on: boolean }
  | { type: "sent"; threadId: string | null }
  | { type: "refused"; threadId: string | null; wasOn: boolean }
  | { type: "unavailable" };

export function webSearchToggleReducer(state: WebSearchToggleState, event: WebSearchToggleEvent): WebSearchToggleState {
  switch (event.type) {
    case "thread":
      return state.threadId === event.threadId ? state : { threadId: event.threadId, on: false };
    case "set":
      // 没有会话就没有可勾选的对象
      if (event.threadId === null) return NO_WEB_SEARCH_TOGGLE;
      return state.threadId === event.threadId && state.on === event.on ? state : { threadId: event.threadId, on: event.on };
    case "sent":
      return state.threadId === event.threadId && state.on ? { ...state, on: false } : state;
    case "refused":
      return event.wasOn && !state.on && state.threadId !== null && state.threadId === event.threadId ? { ...state, on: true } : state;
    case "unavailable":
      return state.on ? { ...state, on: false } : state;
  }
}

/** 这一刻是否按「联网」发送：运维已配好、有会话、勾选属于当前会话。三者缺一即关。 */
export function webSearchActive(state: WebSearchToggleState, threadId: string | null, ready: boolean): boolean {
  return ready && threadId !== null && state.threadId === threadId && state.on;
}

/** 请求体里的值：只有勾选才是 `true`，其余一律 `undefined`——未勾选时请求体里没有 web_search 键。 */
export function messageWebSearchValue(active: boolean): true | undefined {
  return active ? true : undefined;
}

// ---------------------------------------------------------------------------
// 设置页「联网搜索」：只读状态 + 管理员的密钥槽位
// ---------------------------------------------------------------------------

/** 面板上的固定说明：地址、提供方与开关是运维的，被盗的管理员账号不能把用户问题改道。 */
export const WEB_SEARCH_PANEL_NOTICE =
  "联网搜索的开关、提供方与搜索服务地址由运维在部署配置里设置，面板不能修改；这里只显示状态，管理员可以填写搜索服务的密钥。";

export const WEB_SEARCH_USAGE_NOTE =
  "就绪后，用户在输入框旁逐条消息勾选「联网」才会联网：勾选的那条消息里，模型生成的搜索词会发往运维配置的搜索服务；联网得到的网页内容不是规范条文，不能当作规范依据引用。";

export type WebSearchTone = "succeeded" | "not_configured" | "failed";

/** 复用 CapabilityStatus 既有的状态类名。 */
export function webSearchTone(view: WebSearchStatusView): WebSearchTone {
  if (view.error) return "failed";
  return view.ready ? "succeeded" : "not_configured";
}

const DETAIL_TEXT: ReadonlyMap<string, string> = new Map([
  ["provider_missing", "运维还没有选择搜索服务提供方（WATER_AGENT_WEB_SEARCH_PROVIDER）"],
  ["base_url_missing", "运维还没有配置搜索服务地址（WATER_AGENT_WEB_SEARCH_BASE_URL）"],
]);

/** 缺项原因码 → 一句人话；密钥那一项按角色给出下一步（管理员能填，成员只能找管理员）。未登记的码原样带出。 */
export function webSearchDetailText(detail: string | null, canManage: boolean): string | null {
  if (!detail) return null;
  if (detail === "api_key_missing") {
    return canManage ? "搜索服务需要密钥，还没有配置，请在下方填写" : "搜索服务需要密钥，还没有配置，请联系管理员填写";
  }
  return DETAIL_TEXT.get(detail) ?? `未就绪（${detail}）`;
}

export function webSearchSummary(view: WebSearchStatusView): string {
  if (view.error) return "运维的联网搜索配置不合法，联网搜索不可用。";
  if (view.ready) return "已就绪：用户可在输入框旁逐条消息勾选「联网」。";
  if (!view.enabled) return "运维未开启联网搜索（WATER_AGENT_WEB_SEARCH=off）：输入框旁不会出现「联网」开关。";
  const reason = webSearchDetailText(view.detail, view.can_manage);
  return reason ? `已开启，但还不能用：${reason}` : "已开启，但还没有就绪：请联系运维检查部署配置。";
}

/**
 * 配置不合法时的错误条。固定说明所有人可见；服务端给的原因只给管理员——
 * 它可能带着环境变量里的片段，成员改不了也不需要看。
 */
export function webSearchConfigError(view: WebSearchStatusView): { code: string; message: string } | null {
  if (!view.error) return null;
  const fixed = "运维配置的联网搜索环境变量不合法，联网搜索暂不可用；请联系运维检查部署配置。";
  const detail = view.can_manage ? view.error.message : "";
  return { code: view.error.code, message: detail ? `${fixed}（${detail}）` : fixed };
}

export type WebSearchRow = { label: string; value: string; mono: boolean };

function keyValue(view: WebSearchStatusView): string {
  if (!view.provider) return "未设置";
  if (!view.needs_key) return "该提供方无需密钥";
  if (!view.key) return "需要密钥（状态未知）";
  return view.key.configured ? `已配置（${sourceLabel(view.key.source)}）` : "未配置";
}

/** 只读状态行。只有主机名，没有完整地址；密钥只有「是否已配置、来自哪一层」。 */
export function webSearchRows(view: WebSearchStatusView): WebSearchRow[] {
  return [
    { label: "运维开关", value: view.enabled ? "已开启" : "未开启", mono: false },
    { label: "提供方", value: view.provider || "未设置", mono: Boolean(view.provider) },
    { label: "搜索服务主机", value: view.host || "未设置", mono: Boolean(view.host) },
    { label: "密钥", value: keyValue(view), mono: false },
  ];
}

/** 密钥来自运维层（环境变量 / Compose secret）时，面板写入的值不会顶替它——直说，免得填了却不生效。 */
export function webSearchKeySourceNote(key: WebSearchKeyStatus): string | null {
  if (key.source === "env") return "当前生效的密钥来自环境变量（运维设置）：在这里写入的值会落盘，但不会顶替它。";
  if (key.source === "secret_file") {
    return "当前生效的密钥来自 Compose secret 文件（运维设置）：在这里写入的值会落盘，但不会顶替它。";
  }
  return null;
}

/**
 * 密钥表单用的行。写入目标固定是 `WEB_SEARCH_KEY_SLOT`，不取决于服务端回传的名字：
 * 这个页面最多只能写这一把密钥，写别的槽位的通道只在「模型与密钥」页。
 */
export function webSearchKeyRow(key: WebSearchKeyStatus): CredentialRow {
  return credentialRows([{ name: WEB_SEARCH_KEY_SLOT, configured: key.configured, source: key.source, updated_at: null }])[0];
}

/** 状态读取失败时给用户看的话：旧后端没有这个接口（404）和登录失效（401）要说人话，其余沿用服务端的说明。 */
export function webSearchLoadError(code: string, status: number, message: string): { code: string; message: string } {
  if (status === 404) return { code, message: "当前服务端版本没有联网搜索功能，升级后才能使用。" };
  if (status === 401) return { code, message: "登录状态已失效，请刷新页面后重新登录。" };
  return { code, message };
}
