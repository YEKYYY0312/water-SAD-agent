import type { CatalogEntryInput, CatalogModel, CatalogThinking, CatalogTier, ModelCatalogView } from "./modelChoiceApi.ts";

/** 本次运行实际使用的、用户选的模型（来自 finalize 的 payload.model；没选时后端不带该键）。 */
export type ModelCard = { id: string };

export function modelFromFinalPayload(payload: Record<string, unknown>): ModelCard | null {
  const id = payload.model;
  return typeof id === "string" && id !== "" ? { id } : null;
}

/** 档位只表示响应速度与费用倾向——文案不得暗示"进阶 = 更准"（没有任何评测证明这一点）。 */
export const TIER_NOTE = "档位只表示响应速度与费用倾向，不代表回答更准确。引用与计算仍由同一套校验把关。";

const TIER_LABEL: Record<CatalogTier, string> = { fast: "快速档", advanced: "进阶档" };

export function tierLabel(tier: string): string {
  return TIER_LABEL[tier as CatalogTier] ?? tier;
}

const THINKING_LABEL: Record<CatalogThinking, string> = {
  default: "不指定思考开关",
  off: "关闭思考",
  roundtrip: "思考型（暂不支持）",
};

export function thinkingLabel(thinking: string): string {
  return THINKING_LABEL[thinking as CatalogThinking] ?? thinking;
}

/** 输入框旁下拉里能选的模型：部署锁定时一个都没有。 */
export function selectableModels(view: ModelCatalogView | null): CatalogModel[] {
  if (!view || view.locked_reason !== null || view.error !== null) return [];
  return view.models.filter((model) => model.selectable);
}

/** 目录为空或没有可选项时，选择器整个不渲染——此时界面与没有这个功能时逐字相同。 */
export function showModelPicker(view: ModelCatalogView | null): boolean {
  return selectableModels(view).length > 0;
}

export function modelOptionLabel(model: CatalogModel): string {
  return `${model.label}（${tierLabel(model.tier)}）`;
}

/** 某个目录项为什么现在不能选；能选返回 null。 */
export function unselectableReason(model: CatalogModel, lockedReason: ModelCatalogView["locked_reason"]): string | null {
  if (lockedReason !== null) return "对话端点由部署配置管理";
  if (model.enabled === false) return "已停用";
  if (model.thinking === "roundtrip") return "思考型模型，暂不支持";
  if (!model.verified) return "尚未通过兼容性检查";
  return model.selectable ? null : "当前不可选";
}

/** 下拉的值：空串 = 不指定（沿用会话默认，再沿用全局模型）。 */
export const FOLLOW_DEFAULT = "";

export function followDefaultLabel(view: ModelCatalogView | null, sessionDefault: string | null | undefined): string {
  if (sessionDefault) {
    const entry = view?.models.find((model) => model.id === sessionDefault);
    return `沿用本会话默认：${entry ? entry.label : sessionDefault}`;
  }
  return "沿用全局模型";
}

/** 会话默认模型已不在可选集合里（被停用 / 删除 / 变为未验证）：发消息会被拒，界面要提示去清除。 */
export function sessionDefaultProblem(view: ModelCatalogView | null, sessionDefault: string | null | undefined): string | null {
  if (!sessionDefault || !view) return null;
  if (view.locked_reason !== null) return "对话端点已由部署配置接管，本会话默认模型不再生效，发送时会被拒绝；请清除默认。";
  if (selectableModels(view).some((model) => model.id === sessionDefault)) return null;
  return "本会话默认模型已不可用（被停用、删除或未通过检查），发送时会被拒绝；请改选或清除默认。系统不会悄悄换成别的模型。";
}

/** 选中后传给后端的值：没选（空串）→ undefined，请求体里就没有 model 键。 */
export function messageModelValue(choice: string): string | undefined {
  return choice === FOLLOW_DEFAULT ? undefined : choice;
}

/** Inspector「本次模型」卡片。没选过模型（用了全局模型）时不显示——此时本卡与没有这个功能时相同。 */
export function modelCardView(
  card: ModelCard | null,
  view: ModelCatalogView | null,
): { title: string; detail: string; note: string } | null {
  if (!card) return null;
  const entry = view?.models.find((model) => model.id === card.id);
  return {
    title: entry ? entry.label : card.id,
    detail: entry ? `${card.id} · ${tierLabel(entry.tier)}` : card.id,
    note: TIER_NOTE,
  };
}

// ---------------------------------------------------------------------------
// 管理员的目录编辑
// ---------------------------------------------------------------------------

export const MODEL_ID_PATTERN = /^[A-Za-z0-9._:/@+-]+$/;
export const MAX_CATALOG_ENTRIES = 32;
export const MAX_LABEL_LENGTH = 40;

export type CatalogDraftRow = {
  /** 稳定的行 key（改 id 时不换行）。 */
  key: string;
  id: string;
  label: string;
  tier: CatalogTier;
  thinking: CatalogThinking;
  enabled: boolean;
  /** 管理员视图里原有的角色限制与价格：编辑本面板不改它们，原样带回，免得保存时被抹掉。 */
  allowed_roles: string[] | null;
  price: CatalogModel["price"];
  /** 已存在于后端（id 不再允许改，改 id 等于新增一个未验证的模型）。 */
  saved: boolean;
};

export function draftFromView(view: ModelCatalogView): CatalogDraftRow[] {
  return view.models.map((model, index) => ({
    key: `saved-${index}-${model.id}`,
    id: model.id,
    label: model.label,
    tier: model.tier,
    thinking: model.thinking,
    enabled: model.enabled !== false,
    allowed_roles: model.allowed_roles ?? null,
    price: model.price ?? null,
    saved: true,
  }));
}

let nextNewRow = 0;

export function newDraftRow(): CatalogDraftRow {
  nextNewRow += 1;
  return {
    key: `new-${nextNewRow}`,
    id: "",
    label: "",
    tier: "fast",
    thinking: "default",
    enabled: true,
    allowed_roles: null,
    price: null,
    saved: false,
  };
}

/** 行内校验；通过返回 null。后端会再校验一遍，这里只是少一次无谓的往返。 */
export function validateDraft(rows: CatalogDraftRow[]): string | null {
  if (rows.length > MAX_CATALOG_ENTRIES) return `最多 ${MAX_CATALOG_ENTRIES} 个模型`;
  const seen = new Set<string>();
  for (const [index, row] of rows.entries()) {
    const id = row.id.trim();
    const name = `第 ${index + 1} 行`;
    if (!id) return `${name}：请填写模型 id`;
    if (id.length > 200 || !MODEL_ID_PATTERN.test(id)) return `${name}：模型 id 只能含字母、数字和 . _ : / @ + -`;
    if (seen.has(id)) return `${name}：模型 id 重复`;
    seen.add(id);
    const label = row.label.trim();
    if (!label) return `${name}：请填写显示名称`;
    if (label.length > MAX_LABEL_LENGTH) return `${name}：显示名称过长`;
  }
  return null;
}

export function draftToPayload(rows: CatalogDraftRow[]): CatalogEntryInput[] {
  return rows.map((row) => {
    const entry: CatalogEntryInput = {
      id: row.id.trim(),
      label: row.label.trim(),
      tier: row.tier,
      thinking: row.thinking,
      enabled: row.enabled,
    };
    if (row.allowed_roles !== null) entry.allowed_roles = row.allowed_roles;
    if (row.price) entry.price = row.price;
    return entry;
  });
}

/** 草稿与已保存目录是否有差别（保存按钮是否可点）。 */
export function draftChanged(rows: CatalogDraftRow[], view: ModelCatalogView): boolean {
  return JSON.stringify(draftToPayload(rows)) !== JSON.stringify(draftToPayload(draftFromView(view)));
}

const ERROR_TEXT: Record<string, string> = {
  WEB_MODEL_CATALOG_INVALID: "模型目录内容不合法",
  WEB_LLM_ENDPOINT_MANAGED: "对话端点由部署配置管理，目录只读",
  WEB_MODEL_UNKNOWN: "所选模型不在可用目录里（可能已被停用或删除）",
  WEB_MODEL_NOT_VERIFIED: "该模型尚未通过兼容性检查，请管理员在「模型与密钥」页运行检查",
  WEB_MODEL_NOT_CONFIGURED: "没有可用的对话 Key，请管理员先配置",
  WEB_MODEL_FORBIDDEN: "当前账号无权选用该模型",
  WEB_MODEL_LOCKED: "对话端点由部署配置管理，不允许按会话 / 按消息选择模型",
  WEB_MODEL_THINKING_UNSUPPORTED: "该模型是思考型模型，当前版本尚不支持选用",
  WEB_MODEL_UNSUPPORTED: "当前运行环境不支持选择模型",
  WEB_LLM_COMPATIBILITY_RUNNING: "兼容性检查进行中，请稍后",
};

/** 稳定码 → 可直接展示的中文；未知码退回后端给的 message。 */
export function modelErrorMessage(code: string, fallback = "请求未完成"): string {
  return ERROR_TEXT[code] ?? fallback;
}
