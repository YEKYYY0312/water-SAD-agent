/** P5-S47: auth API calls for the React AuthGate.
 *
 * Four endpoints, all same-origin, all going through the shared envelope /
 * CSRF helpers from workspaceApi so there is exactly one place that knows
 * about response shapes and cookie handling.
 */

import { csrfToken, type ApiEnvelope } from "./workspaceApi";

export type AuthStatus =
  | { authenticated: false }
  | {
      authenticated: true;
      user: { user_id: string; username: string; role: string };
      must_change_password: boolean;
    };

export class AuthApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "AuthApiError";
    this.code = code;
    this.status = status;
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET" && init.method !== "HEAD") {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  let envelope: ApiEnvelope<T>;
  try {
    envelope = (await response.json()) as ApiEnvelope<T>;
  } catch {
    throw new AuthApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", response.status);
  }
  if (!response.ok || !envelope.ok || envelope.data === undefined) {
    throw new AuthApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      response.status,
    );
  }
  return envelope.data;
}

export const authApi = {
  status(): Promise<AuthStatus> {
    return call<AuthStatus>("/api/auth/status");
  },

  login(username: string, password: string): Promise<{ must_change_password: boolean }> {
    return call("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  logout(): Promise<{ logged_out: boolean }> {
    return call("/api/auth/logout", { method: "POST" });
  },

  changePassword(currentPassword: string, newPassword: string): Promise<{ changed: boolean }> {
    return call("/api/auth/change-pass", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
  },
};
