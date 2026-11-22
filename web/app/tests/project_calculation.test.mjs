import assert from "node:assert/strict";
import test from "node:test";

import { buildProjectPlan, fieldsFromPlan, ProjectCalculationApi } from "../src/projectCalculation.ts";

const values = {
  flow_lps: "10", diameter_mm: "100", length_m: "100", friction_factor: "0.02",
  gravity_mps2: "9.81", static_head_m: "10", local_head_m: "0", residual_head_m: "2",
};
const head = { base_revision: 1, generation: 1, active_branch_id: "main", active_run_id: "r1" };

test("the plan binds both calculations to the same diameter and routes outputs in order", () => {
  const { plan, base_revision, generation } = buildProjectPlan("project", head, values);
  assert.equal(base_revision, 1);
  assert.equal(generation, 1);
  assert.deepEqual(plan.nodes.map(n => n.depends_on), [[], ["v"], ["hf"]]);
  assert.deepEqual(plan.nodes[0].inputs.diameter_mm, plan.nodes[1].inputs.diameter_mm);
  assert.equal(plan.nodes[2].inputs.friction_head_m.output_key, "head_loss_m");
  assert.equal(plan.nodes[2].inputs.local_head_m.value, 0);
  assert.deepEqual(fieldsFromPlan(plan), values);
});

test("blank, nondecimal and invalid engineering input cannot reach preview", () => {
  for (const [field, value] of [
    ["flow_lps", ""], ["flow_lps", "0x20"], ["flow_lps", "5001"], ["diameter_mm", "0"],
    ["gravity_mps2", "Infinity"], ["residual_head_m", "-0"], ["friction_factor", "-1"],
  ]) {
    assert.throws(() => buildProjectPlan("project", head, { ...values, [field]: value }), RangeError);
  }
});

test("a revision retains the server's plan identifiers", () => {
  const first = buildProjectPlan("project", head, values).plan;
  const next = buildProjectPlan("project", { ...head, active_branch_id: "other", active_plan: {
    ...first, plan_id: "existing", system_id: "wsb", segment_id: "pipe-b",
  } }, { ...values, flow_lps: "12" }).plan;
  assert.equal(next.plan_id, "existing");
  assert.equal(next.system_id, "wsb");
  assert.equal(next.segment_id, "pipe-b");
  assert.equal(next.branch_id, "other");
  assert.equal(next.nodes[0].inputs.flow_lps.value, 12);
  assert.notEqual(next.input_set_id, first.input_set_id);
});

test("execution uses the exact preview token and sends no computed input", async () => {
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    assert.equal(url, "/api/sessions/thread/project-calculations/execute");
    assert.equal(opts.credentials, "same-origin");
    assert.equal(opts.method, "POST");
    assert.deepEqual(JSON.parse(opts.body), { confirmation_id: "id", expected_plan_hash: "hash" });
    return new Response(JSON.stringify({ ok: true, data: { run_id: "run" } }), { status: 202 });
  };
  try {
    assert.equal((await new ProjectCalculationApi().execute("thread", { confirmation_id: "id", plan_hash: "hash" })).run_id, "run");
  } finally { globalThis.fetch = orig; }
});
