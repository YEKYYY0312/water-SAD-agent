"""Skill 下载（P7-S13）：静态注册表协议、隔离区、批准与安装。

**本包只被 ``water_agent.skills.cli`` 导入**（P7-I12，``tests/test_skill_fetch_invariants.py`` 用 AST 判定）：
取回与批准是运维主机 shell 上的人工动作，模型工具、Web 面板与插件都不得导入它。已安装 skill 的**只读**部分
（lock 读取、加载期哈希重算）在 ``water_agent.skills.installed``，那是库与面板唯一可以碰的东西。
"""
