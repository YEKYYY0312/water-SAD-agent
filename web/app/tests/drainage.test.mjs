import assert from "node:assert/strict";
import test from "node:test";
import { DrawingApi } from "../src/drawingApi.ts";
import { drainageFormFrom, drainageRequest } from "../src/drainageState.ts";

const point = (x_mm, y_mm, invert_mm) => ({ x_mm, y_mm, invert_mm });
const example = {
  system_type: "排水", room_width_mm: 2600, room_depth_mm: 2400,
  fixtures: {
    toilet: { ...point(400, 1800, -130), dn_mm: 100 },
    basin: { ...point(1000, 2000, -120), dn_mm: 50 },
    floor_drain: { ...point(400, 200, -140), dn_mm: 50 },
  },
  junction: point(1000, 1000, -150),
  riser: point(2000, 1000, -170),
  collector_dn_mm: 100,
};

test("drainage keeps signed inverts and never turns a blank coordinate into zero", () => {
  const form = drainageFormFrom(example);
  assert.deepEqual(drainageRequest(form, "排水"), example);
  assert.throws(() => drainageRequest({ ...form, "junction.x_mm": "" }, "排水"), RangeError);
  assert.throws(() => drainageRequest({ ...form, "junction.x_mm": "0xff" }, "排水"), RangeError);
  assert.throws(() => drainageRequest({ ...form, "riser.invert_mm": "1.2" }, "排水"), RangeError);
  assert.equal(drainageRequest({ ...form, "riser.invert_mm": "0" }, "排水").riser.invert_mm, 0);
});

test("drainage sends the explicit template and existing CSRF header to generation", async () => {
  const original = globalThis.fetch;
  globalThis.document = { cookie: "wa_csrf=drainage-fixture" };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/drawings/generate");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers.get("X-CSRF-Token"), "drainage-fixture");
    assert.deepEqual(JSON.parse(options.body), example);
    return new Response(JSON.stringify({ ok: true, data: { system_type: "排水", segments: [] } }), { status: 201 });
  };
  try { assert.equal((await new DrawingApi().generateDrainage(example)).system_type, "排水"); }
  finally { globalThis.fetch = original; delete globalThis.document; }
});
