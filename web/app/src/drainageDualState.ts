import type { DualDrainageRequest, DualPipeMark, DualPointKind } from "./drawingApi.ts";

export type DualNodeForm = {
  id: string;
  kind: DualPointKind | "";
  x_mm: string;
  y_mm: string;
  invert_mm: string;
  end: string;
  mark: DualPipeMark | "";
  size_mm: string;
};

export type DualForm = { width: string; depth: string; nodes: DualNodeForm[] };

export function emptyDualPoint(id: string, kind: DualNodeForm["kind"] = ""): DualNodeForm {
  return { id, kind, x_mm: "", y_mm: "", invert_mm: "", end: "", mark: "", size_mm: "" };
}

export function dualFormFrom(example?: DualDrainageRequest): DualForm {
  if (!example) {
    return { width: "", depth: "", nodes: [
      emptyDualPoint("N1"), emptyDualPoint("N2"), emptyDualPoint("S1", "outlet"),
      emptyDualPoint("S2", "outlet"),
    ] };
  }
  const routes = new Map(example.pipes.map((pipe) => [pipe.start, pipe]));
  return {
    width: String(example.room_width_mm), depth: String(example.room_depth_mm),
    nodes: example.nodes.map((node) => {
      const pipe = routes.get(node.id);
      return {
        id: node.id, kind: node.kind, x_mm: String(node.x_mm), y_mm: String(node.y_mm),
        invert_mm: String(node.invert_mm), end: pipe?.end ?? "", mark: pipe?.mark ?? "",
        size_mm: pipe ? String(pipe.size_mm) : "",
      };
    }),
  };
}

function integer(raw: string, label: string): number {
  if (!/^[+-]?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new RangeError(`${label}须填写整数。`);
  }
  return Number(raw);
}

export function dualRequest(form: DualForm, system: string): DualDrainageRequest {
  return {
    system_type: system, topology: "dual_outlet",
    room_width_mm: integer(form.width, "卫生间宽度"), room_depth_mm: integer(form.depth, "卫生间深度"),
    nodes: form.nodes.map((row) => {
      if (row.kind === "") throw new RangeError(`请选择 ${row.id} 的接点类型。`);
      return {
        id: row.id, kind: row.kind,
        x_mm: integer(row.x_mm, `${row.id} 的 X 坐标`),
        y_mm: integer(row.y_mm, `${row.id} 的 Y 坐标`),
        invert_mm: integer(row.invert_mm, `${row.id} 的管底标高`),
      };
    }),
    pipes: form.nodes.flatMap((row) => {
      if (row.kind === "outlet") return [];
      if (!row.end) throw new RangeError(`请选择 ${row.id} 的下游接点。`);
      if (!row.mark) throw new RangeError(`请选择 ${row.id} 的管径写法。`);
      return [{ start: row.id, end: row.end, mark: row.mark, size_mm: integer(row.size_mm, `${row.id} 的管径`) }];
    }),
  };
}
