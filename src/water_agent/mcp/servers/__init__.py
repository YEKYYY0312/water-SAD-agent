"""P4 Step 24 自研 MCP Server 端（水力计算 + 规范检索）。

两台 server 均为 stdio JSON-RPC 实现，可直接被 ``MCPStdioClient`` 连接：

- ``hydraulics_server.py``：7 个水力公式工具，纯计算无 I/O
- ``specs_server.py``：3 个检索工具（检索/精确取条/版本），只读本地语料

均无第三方依赖，stdin/stdout 行分隔 JSON，stderr 打日志。
"""
