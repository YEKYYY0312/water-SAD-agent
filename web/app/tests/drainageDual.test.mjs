import assert from "node:assert/strict";
import test from "node:test";

import { dualFormFrom, dualRequest } from "../src/drainageDualState.ts";

const example = {
  system_type: "排水", topology: "dual_outlet", room_width_mm: 1300, room_depth_mm: 1900,
  nodes: [
    { id: "A", kind: "basin", x_mm: 0, y_mm: 300, invert_mm: 0 },
    { id: "B", kind: "toilet", x_mm: 0, y_mm: 800, invert_mm: -10 },
    { id: "S1", kind: "outlet", x_mm: 1000, y_mm: 300, invert_mm: -40 },
    { id: "S2", kind: "outlet", x_mm: 1000, y_mm: 800, invert_mm: -50 },
  ],
  pipes: [
    { start: "A", end: "S1", mark: "De", size_mm: 75 },
    { start: "B", end: "S2", mark: "DN", size_mm: 100 },
  ],
};

test("dual outlet serialization preserves both endpoints and both diameter notations", () => {
  const form = dualFormFrom(example);
  assert.deepEqual(dualRequest(form, "排水"), example);
  form.nodes[0].invert_mm = "";
  assert.throws(() => dualRequest(form, "排水"), /管底标高/);
  form.nodes[0].invert_mm = "0";
  form.nodes[1].end = "";
  assert.throws(() => dualRequest(form, "排水"), /下游/);
});
