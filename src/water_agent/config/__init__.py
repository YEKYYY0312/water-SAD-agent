"""运行期配置边界：目前只承载 LLM 凭据的解析与写入。

凭据的三条来源（环境变量、Compose secret 文件、可写凭据卷）由
:mod:`water_agent.config.credentials` 统一收敛，调用方不再各自判断。
"""
