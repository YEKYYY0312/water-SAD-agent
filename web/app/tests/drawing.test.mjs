import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { DrawingApi, DrawingApiError } from "../src/drawingApi.ts";
import {
  DRAFT_NOTICE,
  defaultForm,
  drawingErrorMessage,
  dwgLine,
  fieldErrors,
  pipeLine,
  reviewLine,
  toRequest,
  unitsLabel,
  validateForm,
  withBuildingType,
} from "../src/drawingState.ts";

const OPTIONS = {
  system_types: ["给水"],
  building_types: [
    { value: "住宅", unit_label: "户", riser_positions: ["楼梯间", "管道井"], default_riser_position: "楼梯间", routing: "沿墙", detail: "水表井" },
    { value: "办公", unit_label: "用水单元", riser_positions: ["核心筒"], default_riser_position: "核心筒", routing: "沿走廊吊顶", detail: "吊顶内阀门组" },
    { value: "商业", unit_label: "商铺", riser_positions: ["边角"], default_riser_position: "边角", routing: "沿柱网", detail: "泵房接管" },
  ],
  diameters: ["DN15", "DN20", "DN25", "DN32", "DN40", "DN50", "DN65", "DN80", "DN100", "DN125", "DN150"],
  materials: ["PPR", "PE", "钢塑复合", "不锈钢"],
  ranges: { floors: [1, 33], units_per_floor: [1, 8], pressure_mpa: [0.1, 1.0], floor_height_m: [2.4, 6.0] },
  defaults: { floor_height_m: 3.0 },
  dwg: { available: true, experimental: true },
  notice: "程序生成的初稿，需设计人复核",
  rules_version: "p6-s1-2026-09-29",
};

const filled = (overrides = {}) => ({ ...defaultForm(OPTIONS), flow_rate_lps: "2.5", pressure_mpa: "0.35", ...overrides });

test("defaults come from the options and never invent flow or pressure", () => {
  const form = defaultForm(OPTIONS);
  assert.equal(form.building_type, "住宅");
  assert.equal(form.riser_position, "楼梯间");
  assert.equal(form.floor_height_m, "3");
  assert.equal(form.flow_rate_lps, "", "设计流量必须由用户填写");
  assert.equal(form.pressure_mpa, "", "供水压力必须由用户填写");
  assert.equal(form.include_dwg, false, "实验性 DWG 默认不勾选");
});

test("switching building type resets the riser to that type's rule default", () => {
  const form = withBuildingType(filled(), OPTIONS, "办公");
  assert.equal(form.riser_position, "核心筒");
  assert.equal(unitsLabel(OPTIONS, "住宅"), "每层户数");
  assert.equal(unitsLabel(OPTIONS, "商业"), "每层商铺数量");
});

test("local validation mirrors the server contract", () => {
  assert.deepEqual(validateForm(filled(), OPTIONS), {});
  const errors = validateForm(
    filled({ floors: "0", units_per_floor: "2.5", pressure_mpa: "1.2", floor_height_m: "", flow_rate_lps: "-1", branch_dn: "DN65", riser_position: "核心筒" }),
    OPTIONS,
  );
  assert.match(errors.floors, /1~33/);
  assert.match(errors.units_per_floor, /整数/);
  assert.match(errors.pressure_mpa, /0.1~1/);
  assert.match(errors.floor_height_m, /请填写/);
  assert.match(errors.flow_rate_lps, /大于 0/);
  assert.match(errors.branch_dn, /不得大于主管/);
  assert.match(errors.riser_position, /立管位置/);
  assert.match(validateForm(filled({ branch_flow_rate_lps: "3" }), OPTIONS).branch_flow_rate_lps, /不得大于总设计流量/);
});

test("the request maps one to one and omits an empty branch flow", () => {
  const request = toRequest(filled(), OPTIONS);
  assert.deepEqual(request, {
    system_type: "给水",
    building_type: "住宅",
    floors: 6,
    units_per_floor: 2,
    riser_position: "楼梯间",
    main_pipe: { dn: "DN50", material: "PPR" },
    branch_pipe: { dn: "DN25", material: "PPR" },
    flow_rate_lps: 2.5,
    pressure_mpa: 0.35,
    floor_height_m: 3,
    include_dwg: false,
  });
  assert.equal("branch_flow_rate_lps" in request, false, "不填就不发送，后端如实报未校核");
  assert.equal(toRequest(filled({ branch_flow_rate_lps: "0.3" }), OPTIONS).branch_flow_rate_lps, 0.3);
});

test("server field errors land on the matching inputs", () => {
  const { byField, other } = fieldErrors([
    { loc: "floors", type: "greater_than_equal", message: "Input should be greater than or equal to 1" },
    { loc: "main_pipe.dn", type: "enum", message: "Input should be 'DN15' …" },
    { loc: "wall_thickness_mm", type: "extra_forbidden", message: "Extra inputs are not permitted" },
  ]);
  assert.ok(byField.floors && byField.main_dn);
  assert.deepEqual(other, ["wall_thickness_mm：Extra inputs are not permitted"]);
  assert.match(drawingErrorMessage("GEN_UNSUPPORTED_SYSTEM"), /给水/);
  assert.match(drawingErrorMessage("GEN_RENDER_FAILED"), /稍后重试/);
  assert.equal(drawingErrorMessage("NEW_CODE", "原话"), "原话");
});

test("review summary shows the unchecked count verbatim and never claims a pass", () => {
  const sheet = {
    pipe_segments: 5,
    dn_matched: 5,
    review: { pipes_reviewed: 20, findings: 0, not_checked: 1, not_checked_items: [], summary: "", report_text: "" },
    dwg: { status: "discarded", reason: "DWG 回读核对未通过", experimental: true, filename: null, converter_warnings: 3 },
  };
  const line = reviewLine(sheet);
  assert.match(line, /未检查 1 项/);
  assert.match(line, /S50 原样/);
  assert.doesNotMatch(line, /通过|0 缺陷|合规/);
  assert.equal(pipeLine(sheet), "图上管段 5 条，读回核对管径一致 5 条");
  assert.match(dwgLine(sheet), /实验性.*已丢弃/);
  assert.equal(dwgLine({ dwg: null }), null);
});

test("generate posts JSON with the CSRF token and keeps field errors", async () => {
  const calls = [];
  globalThis.document = { cookie: "wa_csrf=token-1" };
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), init });
    return new Response(
      JSON.stringify({ version: "v1", ok: false, error: { code: "GEN_INVALID_INPUT", message: "出图参数不合法", fields: [{ loc: "floors", type: "x", message: "y" }] } }),
      { status: 422 },
    );
  };
  await assert.rejects(
    () => new DrawingApi().generate(toRequest(filled(), OPTIONS)),
    (error) => error instanceof DrawingApiError && error.code === "GEN_INVALID_INPUT" && error.fields[0].loc === "floors",
  );
  assert.equal(calls[0].url, "/api/drawings/generate");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("X-CSRF-Token"), "token-1");
  assert.equal(JSON.parse(calls[0].init.body).building_type, "住宅");
  delete globalThis.fetch;
  delete globalThis.document;
});

test("download is a same-page blob download named from RFC 5987", async () => {
  const clicks = [];
  globalThis.fetch = async (input) => {
    assert.equal(String(input), "/api/drawings/abc/download");
    return new Response(new Blob(["PK"]), {
      status: 200,
      headers: { "Content-Disposition": "attachment; filename=\"abc.zip\"; filename*=UTF-8''%E7%BB%99%E6%B0%B4%E5%9B%BE%E7%BA%B8.zip" },
    });
  };
  globalThis.document = { createElement: () => ({ click() { clicks.push(this.download); }, remove() {} }), body: { append() {} } };
  globalThis.window = { setTimeout: () => 0 };
  const original = URL.createObjectURL;
  URL.createObjectURL = () => "blob:1";
  const name = await new DrawingApi().download({ download: { url: "/api/drawings/abc/download", filename: "fallback.zip", size_bytes: 2 } });
  assert.equal(name, "给水图纸.zip");
  assert.deepEqual(clicks, ["给水图纸.zip"]);
  URL.createObjectURL = original;
  delete globalThis.fetch;
  delete globalThis.document;
  delete globalThis.window;
});

test("the panel states the draft notice and computes no numbers itself", async () => {
  const panel = await readFile(new URL("../src/DrawingPanel.tsx", import.meta.url), "utf8");
  const state = await readFile(new URL("../src/drawingState.ts", import.meta.url), "utf8");
  assert.equal(DRAFT_NOTICE, "程序生成的初稿，需设计人复核");
  assert.match(state, /程序生成的初稿，需设计人复核/);
  assert.match(panel, /DRAFT_NOTICE/);
  assert.match(panel, /record\.note/, "计算结果显示后端给出的原句");
  // 铁律①：数值只来自后端的程序化计算，前端不做任何数值运算或舍入。
  assert.doesNotMatch(`${panel}\n${state}`, /Math\.|toFixed\(/);
  // 枚举不写死：表单词汇全部来自 /api/drawings/options。
  assert.doesNotMatch(panel, /"DN50"|"PPR"|"楼梯间"/);
});

test("workbench keeps the drawing panel mutually exclusive with memory and settings", async () => {
  const source = await readFile(new URL("../src/Workbench.tsx", import.meta.url), "utf8");
  assert.match(source, /DrawingPanel/);
  assert.match(source, /aria-pressed=\{workspace\.drawingOpen\}/);
  // 打开任何一个面板都要关掉另外两个。
  const openers = source.split("const open").slice(1).map((chunk) => chunk.slice(0, chunk.indexOf("};")));
  assert.equal(openers.length, 3);
  for (const opener of openers) {
    const closes = ["setMemoryOpen(false)", "setSettingsOpen(false)", "setDrawingOpen(false)"].filter((call) => opener.includes(call));
    assert.equal(closes.length, 2, opener.slice(0, 40));
  }
});
