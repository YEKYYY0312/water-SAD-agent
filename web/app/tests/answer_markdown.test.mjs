import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { imageAsText } from "../src/answerImages.ts";

test("P7-S11-A08 an image becomes plain text with only the host name — never a request, never a link", () => {
  const text = imageAsText("示意图", "https://evil.example/p?q=%E7%A7%81%E6%9C%89&token=abc#frag");
  assert.equal(text, "［示意图：外部图片未加载，来源 evil.example］");
  // 路径、查询串、令牌都不出现在界面文字里
  for (const leaked of ["/p", "q=", "token", "abc", "frag", "https://"]) {
    assert.ok(!text.includes(leaked), leaked);
  }
});

test("P7-S11-A08 missing alt, missing src and unparsable src all degrade to a safe placeholder", () => {
  assert.equal(imageAsText(undefined, undefined), "［图片：图片未加载］");
  assert.equal(imageAsText("  ", "not a url"), "［图片：图片未加载］");
  assert.equal(imageAsText("图", "data:image/png;base64,AAAA"), "［图：图片未加载］");
  assert.equal(imageAsText("图", "javascript:alert(1)"), "［图：图片未加载］");
});

test("P7-S11-A08 the renderer overrides img, keeps skipHtml, and links stay noopener noreferrer nofollow", async () => {
  const source = await readFile(new URL("../src/AnswerMarkdown.tsx", import.meta.url), "utf8");
  // 覆盖 img：不再让 react-markdown 默认生成 <img>
  assert.match(source, /img:\s*\(\{ alt, src \}\)/);
  assert.match(source, /imageAsText\(/);
  // 只看代码、不看注释（注释里会提到 `<img>`）
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(code, /<img\b/, "组件里不能自己渲染 <img>");
  // 既有边界不退化
  assert.match(source, /skipHtml/);
  assert.match(source, /rel="noopener noreferrer nofollow"/);
  assert.doesNotMatch(source, /rehype-raw|dangerouslySetInnerHTML/);
});
