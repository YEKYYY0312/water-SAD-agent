import { useCallback, useEffect, useState, type ReactNode } from "react";

import { authApi, type AuthStatus } from "./authApi";
import { ChangePasswordPanel } from "./ChangePasswordPanel";
import { LoginPanel } from "./LoginPanel";

type GateState = "loading" | "login" | "change-password" | "ready";

/** 会话中途过期时广播的事件名：Workbench 的 401 兜底调 window.dispatchEvent。 */
export const AUTH_EXPIRED_EVENT = "water-agent:auth-expired";

/**
 * P5-S47 认证门：渲染 Workbench 之前的唯一入口。
 *
 * 三态判定完全由 /api/auth/status 承载（authenticated + must_change_password），
 * 不借用 HTTP 状态码——401/403 已被「会话失效」「须改密」占用，前端无法区分。
 *
 * 会话中途过期：Workbench 的 401 兜底 dispatch AUTH_EXPIRED_EVENT，
 * 本组件监听后回到登录面板。不做自动跳转是因为那会弄丢用户正在输入的内容；
 * 只有用户显式点击「返回登录」才触发。
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>("loading");

  useEffect(() => {
    let active = true;
    void authApi
      .status()
      .then((status: AuthStatus) => {
        if (!active) return;
        if (!status.authenticated) {
          setState("login");
        } else if (status.must_change_password) {
          setState("change-password");
        } else {
          setState("ready");
        }
      })
      .catch(() => {
        // 探针本身失败（网络/服务端未起）按未登录处理：用户看到登录页，
        // 登录提交时会得到真正的网络错误，比白屏更可行动。
        if (active) setState("login");
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const onExpired = () => setState("login");
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  switch (state) {
    case "loading":
      return (
        <main className="auth-gate" aria-busy="true">
          <p className="muted-copy">正在确认登录状态…</p>
        </main>
      );
    case "login":
      return (
        <LoginPanel
          onLoggedIn={(mustChangePassword) =>
            setState(mustChangePassword ? "change-password" : "ready")
          }
        />
      );
    case "change-password":
      return <ChangePasswordPanel onChanged={() => setState("ready")} />;
    case "ready":
      return <>{children}</>;
  }
}
