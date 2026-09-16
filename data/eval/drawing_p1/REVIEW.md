# Drawing P1 复核记录（终：目视复核关闭，0 缺陷）

**结论（2026-09-16）**：18 张真实图纸经 CAD 目视复核，**均未发现规范违规**。
文本候选 28 条全部撤回：文本复核删 7 条（配件规格/设备接口/孤立文本），目视复核删 21 条（管径/材料/坡度观测均合规）。

## 用途裁定

- `data/eval/drawing_p1_gold.jsonl`：0 条 gold 缺陷占位（`review_status=visual_review_closed_no_defects`）。
- `data/eval/drawing_p1/negative_set.jsonl`：28 条误报负例，用于误报率（FP）测试。
- 两文件 `accepts_recall_gate=false`：**不构成召回率验收证据**，不可作 recall 分母。
- 召回率门禁仍需带已知缺陷图纸或植入缺陷副本（blocked）。

## 口径提醒

- 目视对象为 DWG/DXF 图纸本身；P3 链路读 ODA 转换件且不展开 INSERT（MANIFEST：含 INSERT 16/18），链路盲区仍在。
- 逐图纸候选数: {"P-01": 9, "P-06": 1, "P-07": 10, "PUMP-01": 3, "S-01": 1, "S-03": 3, "S-04": 1}
