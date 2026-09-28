import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("workbench preserves the engineering workspace boundary", async () => {
  const source = await readFile(new URL("../src/Workbench.tsx", import.meta.url), "utf8");
  const memorySource = await readFile(new URL("../src/MemoryWorkspace.tsx", import.meta.url), "utf8");
  // P5-S48：品牌区随顶栏一起并入侧栏，标题现在由 ProjectSidebar 渲染。
  const sidebarSource = await readFile(new URL("../src/workspaceComponents.tsx", import.meta.url), "utf8");

  assert.match(sidebarSource, /给排水 Agent 工作台/);
  assert.match(source, /ProjectSidebar/);
  assert.match(source, /MemoryWorkspace/);
  assert.match(memorySource, /role="log"/);
  assert.match(memorySource, /不从 trace 推断正文/);
  assert.doesNotMatch(`${source}\n${memorySource}`, /Math\.(?:round|pow|sqrt)/);
});
