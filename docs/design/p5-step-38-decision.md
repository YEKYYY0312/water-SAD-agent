# P5 Step 38 决策记录：PostgreSQL 业务存储与 LangGraph checkpoint

## 结论

Web 持久化边界使用 SQLAlchemy 2 + psycopg 3 连接池，schema 由 Alembic
`0001_step38` 唯一维护。业务表包含 `users`、`auth_sessions`、`projects`、
`project_memberships`、`web_sessions` 和 `runs`；长期记忆表留给 Step 39。
LangGraph 使用官方 `langgraph-checkpoint-postgres` 的 `PostgresSaver`，但不调用
其会创建表的 `setup()`，避免形成第二套 migration 真源。

## 身份与隔离

`thread_id` 只作为服务端生成的会话 UUID。项目、会话和 run repository 的查询都
把认证用户作为 SQL 条件，并对项目 membership 做 `EXISTS` 授权过滤；资源 ID 猜测
只会得到统一的 `ResourceNotFoundError`。业务写入在一个事务中完成，异常统一回滚。

## 启动与 ready

Compose 的 `migrate` 一次性任务先于 `app` 启动；Alembic 失败返回非零，app 不会被
Compose 拉起。`/health/ready` 检查数据库连通性、`alembic_version` 是否为
`0001_step38`，并检查 LangGraph `checkpoint_migrations` 是否为当前锁定版本的 head；
全程不调用收费模型。未配置数据库的默认进程保持存活但返回 `status=not_ready`。

## 兼容与边界

CLI 的 `water_agent.session`、SQLite checkpointer 和现有 legacy/agentic/docgen/P3/P4
路径未改动。2026-09-12 使用隔离 tmpfs 容器中的 PostgreSQL 16 实测 Alembic migration、
认证和 Web 会话重启恢复、业务事务回滚、跨用户 checkpoint/元数据隔离与 checkpoint
跨进程恢复；CI service 已配置为使用与 Compose 一致的固定 PostgreSQL 18.2 镜像执行同一测试。
未配置 `P5_TEST_DATABASE_URL` 时集成测试明确 SKIP，不包装成通过。

认证 audit sink 和登录限流仍是 Step 37 的日志/单进程实现，本步未新增 audit 或限流表；
默认 Compose 仍限制为单 app 实例。固定 PostgreSQL 18.2 镜像因当前 Docker Hub/官方源码
端点网络超时未能在本机拉取，当前本机真实数据库证据仅覆盖 PostgreSQL 16，18.2 由 CI
service 留下可执行验证配置，不能把本机 16 的结果表述为 18.2 已验证。
Compose 的命名卷挂到 PostgreSQL 18 的 `/var/lib/postgresql` 根目录，以承载镜像默认的
版本化 `PGDATA` 子目录；镜像升级仍须先执行备份与恢复演练，不能原地跨主版本复用数据目录。

## 依赖证据

锁定范围为 `sqlalchemy>=2.0,<3`、`alembic>=1.13,<2`、`psycopg>=3.2,<4`、
`psycopg-binary>=3.2,<4`、`psycopg-pool>=3.2,<4` 和
`langgraph-checkpoint-postgres>=3.0,<4`；当前最小安装解析到 SQLAlchemy 2.0.52、
Alembic 1.20.0、psycopg 3.3.5、langgraph-checkpoint-postgres 3.1.2。SQLAlchemy
为 MIT；已安装 wheel 内许可证文件确认 Alembic 与 langgraph-checkpoint-postgres 为
MIT，psycopg、psycopg-binary、psycopg-pool 为 LGPL。wheel 元数据未统一提供 SPDX 字段，
发布清单仍须保留对应许可证正文与版本映射。
