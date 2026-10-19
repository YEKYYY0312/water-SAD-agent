"""外部内容获取的共同底座（P7-S11）：信封与归一化、受治理的 GET 内核、回合污点。

本包里**没有任何原始网络原语**：出站只经 ``external/http_get.py::governed_get``，后者只调用
``mcp/http_transport.py::open_exchange``（P7-I08 约束它的调用方）。
"""
