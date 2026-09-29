import { useState, type FormEvent } from "react";

import { authApi, AuthApiError } from "./authApi";
import { authErrorText, validatePasswordChange } from "./authState";

type ChangePasswordPanelProps = {
  /** 改密成功后回调：AuthGate 重新拉 status，应已进入工作台。 */
  onChanged: () => void;
};

/**
 * P5-S47 强制改密面板：must_change_password=true 的用户登录后唯一可达的界面。
 * 后端改密不重置会话（见 auth/routes.py），成功后 status 即可放行。
 */
export function ChangePasswordPanel({ onChanged }: ChangePasswordPanelProps) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const complaint = validatePasswordChange(currentPassword, newPassword, confirmPassword);
    if (complaint) {
      setError(complaint);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await authApi.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      onChanged();
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
      <section className="auth-card" aria-labelledby="change-password-heading">
        <div className="brand auth-brand" aria-label="给排水 Agent">
          <span className="brand-mark" aria-hidden="true">WA</span>
          <div>
            <h1 id="change-password-heading">首次登录请修改密码</h1>
            <p>设置一个只有你知道的新密码</p>
          </div>
        </div>

        {error && (
          <div className="dialog-error" role="alert">
            <span>{error}</span>
          </div>
        )}

        <form className="dialog-form" onSubmit={submit}>
          <label htmlFor="current-password">当前密码</label>
          <input
            id="current-password"
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
            required
          />

          <label htmlFor="new-password">新密码</label>
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            required
          />

          <label htmlFor="confirm-password">确认新密码</label>
          <input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            required
          />

          <div className="dialog-actions">
            <button type="submit" className="button-primary" disabled={busy}>
              {busy ? "提交中…" : "修改密码"}
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
