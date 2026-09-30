import assert from "node:assert/strict";
import test from "node:test";

import {
  authErrorText,
  validateLoginInput,
  validatePasswordChange,
} from "../src/authState.ts";

test("login validation blocks the empty shapes before a network round-trip", () => {
  assert.match(validateLoginInput("", "x"), /用户名/);
  assert.match(validateLoginInput("   ", "x"), /用户名/);
  assert.match(validateLoginInput("admin", ""), /密码/);
  assert.equal(validateLoginInput("admin", "secret"), null);
});

test("password-change validation: equality and sameness, not strength", () => {
  // 强度由后端 AUTH_WEAK_PASSWORD 裁定；前端只拦「不一致」与「新旧相同」这两类
  // 纯本地可判定的错误，其余一律交给后端，避免两套强度规则漂移。
  assert.match(validatePasswordChange("", "a", "a"), /当前密码/);
  assert.match(validatePasswordChange("old", "", ""), /新密码/);
  assert.match(validatePasswordChange("old", "abc", "abd"), /不一致/);
  assert.match(validatePasswordChange("same", "same", "same"), /不能与当前密码相同/);
  assert.equal(validatePasswordChange("old", "new-pass", "new-pass"), null);
});

test("authErrorText maps stable backend codes to actionable Chinese copy", () => {
  assert.equal(authErrorText("AUTH_INVALID_CREDENTIALS", "Invalid credentials"), "用户名或密码不正确");
  assert.equal(authErrorText("AUTH_RATE_LIMITED", "x"), "失败次数过多，请稍后再试");
  assert.equal(authErrorText("AUTH_USER_INACTIVE", "x"), "账号已停用，请联系管理员");
  assert.equal(authErrorText("AUTH_SESSION_INVALID", "x"), "会话已失效，请重新登录");
  // AUTH_WEAK_PASSWORD 透传后端文案（含最小长度），不做前端重写。
  assert.equal(authErrorText("AUTH_WEAK_PASSWORD", "Password must be at least 12 characters"), "Password must be at least 12 characters");
  // 后端同一条"新旧相同"规则的错误码（API 直调时出现）与前端本地检查同文案。
  assert.equal(authErrorText("AUTH_PASSWORD_REUSED", "x"), "新密码不能与当前密码相同");
  // 未列出的 code 原样透传 message。
  assert.equal(authErrorText("SOME_FUTURE_CODE", "原始消息"), "原始消息");
});
