import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 回答正文的 Markdown 渲染（P5-S49）：模型输出的 `**加粗**`、列表、表格此前按纯文本显示。
 *
 * 安全边界：react-markdown 默认**不渲染原始 HTML**（这里也不接任何原始 HTML 插件），
 * 模型或上传文件里混进的 `<script>` / `<img onerror>` 只会作为文字出现。
 * 链接一律新窗口打开并切断 opener，避免回答里的链接接管工作台页面。
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
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
