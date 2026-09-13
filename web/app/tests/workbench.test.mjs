import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("workbench preserves the engineering workspace boundary", async () => {
  const source = await readFile(new URL("../src/Workbench.tsx", import.meta.url), "utf8");
  const memorySource = await readFile(new URL("../src/MemoryWorkspace.tsx", import.meta.url), "utf8");

  assert.match(source, /给排水 Agent 工作台/);
  assert.match(source, /MemoryWorkspace/);
  assert.match(memorySource, /role="log"/);
  assert.match(memorySource, /不从 trace 推断正文/);
  assert.doesNotMatch(`${source}\n${memorySource}`, /Math\.(?:round|pow|sqrt)/);
});
