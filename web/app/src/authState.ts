/** P5-S47: pure auth-state helpers, kept UI-free so node --test can cover them. */

/** 登录表单提交前的最小前端检查；强度与格式由后端裁定（AUTH_INVALID_CREDENTIALS / AUTH_RATE_LIMITED）。 */
export function validateLoginInput(username: string, password: string): string | null {
  if (!username.trim()) return "请填写用户名";
  if (!password) return "请填写密码";
  return null;
}

/** 改密表单的前端检查：只做「非空 + 两次一致」，长度强度由后端 AUTH_WEAK_PASSWORD 裁定。 */
export function validatePasswordChange(
  currentPassword: string,
  newPassword: string,
  confirmPassword: string,
): string | null {
  if (!currentPassword) return "请填写当前密码";
  if (!newPassword) return "请填写新密码";
  if (newPassword !== confirmPassword) return "两次输入的新密码不一致";
  if (newPassword === currentPassword) return "新密码不能与当前密码相同";
  return null;
}

/** 后端认证错误的用户可读文案；未列出的 code 原样透传 message。 */
export function authErrorText(code: string, message: string): string {
  switch (code) {
    case "AUTH_INVALID_CREDENTIALS":
      return "用户名或密码不正确";
    case "AUTH_RATE_LIMITED":
      return "失败次数过多，请稍后再试";
    case "AUTH_USER_INACTIVE":
      return "账号已停用，请联系管理员";
    case "AUTH_WEAK_PASSWORD":
      return message; // 后端文案已含最小长度，直接透传
    case "AUTH_SESSION_INVALID":
      return "会话已失效，请重新登录";
    default:
      return message;
  }
}
