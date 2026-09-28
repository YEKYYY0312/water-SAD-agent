import { WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";
import type { WorkspaceRunState } from "./workspaceState";

export type ExportFormat = "docx" | "xlsx";

/**
 * 下载按钮是否显示（spec D4）：只在 succeeded 后出现。
 * - Word：有回答正文；
 * - Excel：本次运行里 material_stats 成功返回过——只有材料清单才有表可导。
 *   计算结果（calc_velocity）进 Word 计算书，不单独出 Excel。
 */
export function availableExports(run: Pick<WorkspaceRunState, "status" | "runId" | "answer" | "tools">): ExportFormat[] {
  if (run.status !== "succeeded" || !run.runId) return [];
  const formats: ExportFormat[] = [];
  if (run.answer.trim()) formats.push("docx");
  if (run.tools.some((tool) => tool.tool_name === "material_stats" && tool.status === "succeeded")) formats.push("xlsx");
  return formats;
}

const EXPORT_ERROR_COPY: Record<string, string> = {
  WEB_EXPORT_UNAVAILABLE: "这次回答已不在服务器内存中（服务重启过），请重新提问后再下载。",
  WEB_EXPORT_SELF_CHECK_FAILED: "回答引用了未经检索的条款，不能生成正式文档。",
  WEB_EXPORT_FORMAT_UNSUPPORTED: "不支持该文件格式。",
  WEB_FORBIDDEN: "只有提问人可以下载这份回答。",
  WEB_NOT_FOUND: "找不到这次运行。",
  WEB_EXPORT_FAILED: "文件生成失败，请稍后重试。",
  WEB_EXPORT_STORAGE_UNAVAILABLE: "服务器的导出目录不可写，请联系管理员。",
};

export function exportErrorMessage(code: string, fallback = "下载未完成"): string {
  return EXPORT_ERROR_COPY[code] ?? fallback;
}

/** 从 RFC 5987 filename*= 取中文文件名，缺失时回落 ASCII filename=。 */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1]);
    } catch {
      /* 落到 ASCII 分支 */
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : fallback;
}

export class ExportApi {
  /** 取回文件。失败时抛带服务端错误码的 WorkspaceApiError（422 的 message 是问题清单）。 */
  async fetchExport(runId: string, format: ExportFormat): Promise<{ blob: Blob; filename: string }> {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/export?format=${format}`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      let envelope: ApiEnvelope<never> | null = null;
      try {
        envelope = (await response.json()) as ApiEnvelope<never>;
      } catch {
        envelope = null;
      }
      throw new WorkspaceApiError(
        envelope?.error?.code ?? "WEB_REQUEST_FAILED",
        envelope?.error?.message ?? "下载未完成",
        response.status,
      );
    }
    return {
      blob: await response.blob(),
      filename: filenameFromDisposition(response.headers.get("Content-Disposition"), `export.${format}`),
    };
  }

  /** 触发浏览器下载，不跳转页面。 */
  async download(runId: string, format: ExportFormat): Promise<string> {
    const { blob, filename } = await this.fetchExport(runId, format);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    // 同步 revoke 会在部分浏览器里取消尚未开始的下载。
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return filename;
  }
}
