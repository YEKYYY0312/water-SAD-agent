import assert from "node:assert/strict";
import test from "node:test";
import { loginPresentation } from "../src/authState.ts";

test("desktop login explains the password set in the App without deployment commands", () => {
  const copy = loginPresentation("Mozilla/5.0 AppleWebKit/605.1.15 WaterAgentDesktop/0.1");
  assert.equal(copy.desktop, true);
  assert.match(copy.passwordPlaceholder, /首次/);
  assert.match(copy.hint, /App/);
  assert.doesNotMatch(copy.hint + copy.passwordPlaceholder, /secrets|pbcopy|cat /);
});

test("ordinary browsers retain the existing deployment login explanation", () => {
  const copy = loginPresentation("Mozilla/5.0 Safari/605.1.15");
  assert.equal(copy.desktop, false);
  assert.equal(copy.passwordPlaceholder, "见 secrets/admin_password");
});
