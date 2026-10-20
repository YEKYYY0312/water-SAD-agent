import { decode, type CredentialSource } from "./settingsApi.ts";

/** 搜索服务密钥槽位的状态。provider 不需要密钥时整个 `key` 是 null。 */
export type WebSearchKeyStatus = { name: string; configured: boolean; source: CredentialSource };

/**
 * 联网搜索的只读状态（P7-S12，`GET /api/settings/web-search`）。
 *
 * 开关、提供方与地址是运维在部署配置里写死的，这里只有状态：主机名而不是完整地址，密钥只有「是否已配置」。
 * `detail` 是缺项原因码（provider_missing / base_url_missing / api_key_missing）；`error` 非空表示运维的环境变量不合法。
 */
export type WebSearchStatusView = {
  enabled: boolean;
  /** 运维已配好、用户可以逐条消息勾选「联网」。 */
  ready: boolean;
  provider: string | null;
  host: string | null;
  needs_key: boolean;
  key: WebSearchKeyStatus | null;
  detail: string | null;
  /** 只有管理员为 true：决定要不要显示密钥表单。 */
  can_manage: boolean;
  error: { code: string; message: string } | null;
};

/**
 * 只有读：没有任何写「地址 / 提供方 / 开关」的方法——这些是运维的，被盗的管理员账号不能把用户问题改道。
 * 密钥的写入与清除走既有的 `SettingsApi.putCredential / deleteCredential`。
 */
export class WebSearchApi {
  async get(): Promise<WebSearchStatusView> {
    const response = await fetch("/api/settings/web-search", { credentials: "same-origin" });
    return decode<WebSearchStatusView>(response);
  }
}
