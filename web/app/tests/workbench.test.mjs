import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("workbench preserves the engineering workspace boundary", async () => {
  const source = await readFile(new URL("../src/Workbench.tsx", import.meta.url), "utf8");

  assert.match(source, /给排水 Agent 工作台/);
  assert.match(source, /role="log"/);
  assert.match(source, /程序化内核/);
  assert.match(source, /Step 37/);
  assert.doesNotMatch(source, /Math\.(?:round|pow|sqrt)/);
});
