import { SettingsApiError } from "./settingsApi.ts";
import { csrfToken } from "./workspaceApi.ts";

/** 模型目录项（P7-S15）。成员只看得到身份字段；管理员多出 enabled / allowed_roles / price。 */
export type CatalogTier = "fast" | "advanced";
export type CatalogThinking = "default" | "off" | "roundtrip";

export type CatalogModel = {
  id: string;
  label: string;
  tier: CatalogTier;
  thinking: CatalogThinking;
  /** 在当前端点上是否已通过逐对兼容性检查。 */
  verified: boolean;
  /** 现在能不能被选：启用 + 非思考型 + 已验证 + 部署没有锁定。 */
  selectable: boolean;
  enabled?: boolean;
  allowed_roles?: string[] | null;
  price?: { input_per_1m: number; output_per_1m: number; cached_input_per_1m?: number | null } | null;
};

export type ModelCatalogView = {
  can_manage: boolean;
  editable: boolean;
  locked_reason: "deployment" | "local_model" | null;
  models: CatalogModel[];
  error: { code: string; message: string } | null;
};

/** 写目录时提交的一项：只有这些字段；端点地址与密钥不属于目录（后端也会拒绝）。 */
export type CatalogEntryInput = {
  id: string;
  label: string;
  tier: CatalogTier;
  thinking: CatalogThinking;
  enabled: boolean;
  allowed_roles?: string[] | null;
  price?: CatalogModel["price"];
};

type Envelope<T> = {
  version: string;
  ok: boolean;
  data?: T;
  error?: { code: string; message: string };
};

async function decode<T>(response: Response): Promise<T> {
  let envelope: Envelope<T>;
  try {
    envelope = (await response.json()) as Envelope<T>;
  } catch {
    throw new SettingsApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", response.status);
  }
  if (!response.ok || !envelope.ok || envelope.data === undefined) {
    throw new SettingsApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      response.status,
    );
  }
  return envelope.data;
}

export class ModelCatalogApi {
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    return decode<T>(response);
  }

  get(): Promise<ModelCatalogView> {
    return this.request("/api/settings/llm-models");
  }

  /** 整份替换目录（管理员）。 */
  put(models: CatalogEntryInput[]): Promise<ModelCatalogView> {
    return this.request("/api/settings/llm-models", { method: "PUT", body: JSON.stringify({ models }) });
  }

  /** 对目录里的某个模型运行兼容性检查（会产生少量计费调用）；202 后重新 get() 看 verified。 */
  runCompatibility(model: string): Promise<{ check: { running: boolean } }> {
    return this.request("/api/settings/llm-endpoint/compatibility", {
      method: "POST",
      body: JSON.stringify({ model }),
    });
  }
}
