import type { DrainageFixtureKey, DrainagePoint, DrainageRequest } from "./drawingApi.ts";

const fixtureLabels = { toilet: "坐便器", basin: "洗手盆", floor_drain: "地漏" } as const;
const pointFields = { x_mm: "X 坐标", y_mm: "Y 坐标", invert_mm: "管底标高" } as const;
export type DrainageForm = Record<string, string>;

export function drainageFormFrom(example?: DrainageRequest): DrainageForm {
  const form: DrainageForm = {};
  if (!example) return form;
  form.room_width_mm = String(example.room_width_mm);
  form.room_depth_mm = String(example.room_depth_mm);
  form.collector_dn_mm = String(example.collector_dn_mm);
  for (const [name, point] of Object.entries(example.fixtures)) {
    for (const [key, number] of Object.entries(point)) form[`fixtures.${name}.${key}`] = String(number);
  }
  for (const node of ["junction", "riser"] as const) {
    for (const [key, number] of Object.entries(example[node])) form[`${node}.${key}`] = String(number);
  }
  return form;
}

export function drainageRequest(form: DrainageForm, system: string): DrainageRequest {
  function integer(key: string, label: string): number {
    const raw = form[key]?.trim() ?? "";
    const value = Number(raw);
    if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(value)) {
      throw new RangeError(`请填写${label}，单位为整数毫米。`);
    }
    return value;
  }
  function point(prefix: string, label: string): DrainagePoint {
    const read = (key: keyof DrainagePoint) => integer(`${prefix}.${key}`, `${label}${pointFields[key]}`);
    return { x_mm: read("x_mm"), y_mm: read("y_mm"), invert_mm: read("invert_mm") };
  }
  function fixture(name: DrainageFixtureKey) {
    const prefix = `fixtures.${name}`;
    return { ...point(prefix, fixtureLabels[name]), dn_mm: integer(`${prefix}.dn_mm`, `${fixtureLabels[name]}支管 DN`) };
  }
  return {
    system_type: system,
    room_width_mm: integer("room_width_mm", "卫生间宽度"),
    room_depth_mm: integer("room_depth_mm", "卫生间深度"),
    fixtures: { toilet: fixture("toilet"), basin: fixture("basin"), floor_drain: fixture("floor_drain") },
    junction: point("junction", "汇合点"),
    riser: point("riser", "立管接驳点"),
    collector_dn_mm: integer("collector_dn_mm", "合流管 DN"),
  };
}
