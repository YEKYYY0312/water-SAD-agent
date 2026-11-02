---
name: pump-room-checklist
description: 运维补充的泵房设计核对顺序。用户要求按泵房核对清单逐项检查时使用。
version: 1.0.0
triggers: [泵房核对, 泵房清单]
requires_tools: [retrieve_clauses, cite_clauses]
---
# 泵房设计核对顺序

1. 先用 retrieve_clauses 检索与泵房布置、吸水管、出水管相关的条文，逐项列出检索到的依据。
2. 每一项结论都要经 cite_clauses 声明引用；检索不到依据的项目如实写"未检索到依据"，不给结论。
3. 涉及流速等数值时，只复述计算工具的输出，不自行估算。
4. 用户没有提供的设计参数，列出缺什么、为什么需要，不代填默认值。
