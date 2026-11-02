---
name: needs-missing-tool
description: 要求调用本部署里不存在的工具的运维 skill。
version: 1.0.0
triggers: [远程报价]
requires_tools: [quote_supplier_prices]
---
# 远程报价

调用 quote_supplier_prices 查询报价后整理成表。
