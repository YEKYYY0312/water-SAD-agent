import { useState, type FormEvent } from "react";

import { authApi, AuthApiError } from "./authApi";
import { authErrorText, validateLoginInput } from "./authState";

type LoginPanelProps = {
  /** 登录成功后回调；must_change_password=true 时由 AuthGate 决定下一步。 */
  onLoggedIn: (mustChangePassword: boolean) => void;
};

/** P5-S47 登录面板：同源提交，失败渲染后端稳定错误码。 */
export function LoginPanel({ onLoggedIn }: LoginPanelProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const complaint = validateLoginInput(username, password);
    if (complaint) {
      setError(complaint);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await authApi.login(username.trim(), password);
      // 密码即刻丢弃：已换取会话 cookie，留在 DOM 里只会增加被旁人看到的机会。
      setPassword("");
      onLoggedIn(result.must_change_password);
    } catch (caught) {
      if (caught instanceof AuthApiError) {
        setError(authErrorText(caught.code, caught.message));
      } else {
        setError("网络错误，请检查服务端是否已启动");
      }
      setBusy(false);
    }
  };

  return (
    <main className="auth-gate">
      <section className="auth-card" aria-labelledby="login-heading">
        <div className="brand auth-brand" aria-label="给排水 Agent">
          <span className="brand-mark" aria-hidden="true">WA</span>
          <div>
            <h1 id="login-heading">给排水 Agent 工作台</h1>
            <p>规范问答 · 计算 · 审查</p>
          </div>
        </div>

        {error && (
          <div className="dialog-error" role="alert">
            <span>{error}</span>
          </div>
        )}

        <form className="dialog-form" onSubmit={submit}>
          <label htmlFor="login-username">用户名</label>
          <input
            id="login-username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />

          <label htmlFor="login-password">密码</label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            placeholder="见 secrets/admin_password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />

          <div className="dialog-actions">
            <button type="submit" className="button-primary" disabled={busy}>
              {busy ? "登录中…" : "登录"}
            </button>
          </div>
        </form>

        <p className="muted-copy auth-hint">
          管理员密码存放在 <code>secrets/admin_password</code>，可复制：
          <code>cat secrets/admin_password | pbcopy</code>
        </p>
      </section>
    </main>
  );
}
