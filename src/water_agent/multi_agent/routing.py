"""Step 32 deterministic router — owner enum in, routing decision out.

任务书 §7.1 路由原则（单一权威）+ 决策记录 D2（循环上限 24/2/2/2/3 冻结）：

1. ``ReviewIssue.owner`` 是路由**唯一输入**；自由文本（message/object_ref/evidence）
   不参与责任推断 —— 篡改 message 不改变 next_owner（路由测试锁死）。
2. 同轮存在多个 owner 时按 ``hydraulic → design → drawing → human`` 依赖顺序一次
   只选一个；上游修订使下游产物 stale（provenance.stale_propagation 语义），
   下游问题留待 stale 重建后的下一轮审查。
3. 循环有界：``MAX_REVIEW_ROUNDS`` / ``MAX_*_REVISIONS`` 任一耗尽 →
   ``blocked=True`` + ``MULTI_RETRY_EXHAUSTED``，不再调用责任 Agent。
4. ``issues=[]`` 且 ``not_checked=[]`` → ``next_owner=None``（decision=pass，
   下一跳是人工批准，不是任何 Agent）；``not_checked`` 非空时上游不得 pass。

本模块是纯函数：零 IO、零 LLM、零环境依赖，离线可测；同输入同输出。
"""

from __future__ import annotations

from typing import TypedDict

from water_agent.multi_agent.contracts import (
    MULTI_RETRY_EXHAUSTED,
    ReviewIssue,
    ReviewPackage,
)

#: 循环上限（决策记录 D2 冻结；调整必须同步改配置版本 + 测试 + baseline）。
MAX_MULTI_AGENT_STEPS = 24
MAX_HYDRAULIC_REVISIONS = 2
MAX_DESIGN_REVISIONS = 2
MAX_DRAWING_REVISIONS = 2
MAX_REVIEW_ROUNDS = 3

#: 多 owner 依赖顺序（任务书 §7.1.2）：hydraulic → design → drawing → human。
OWNER_PRIORITY: tuple[str, str, str, str] = ("hydraulic", "design", "drawing", "human")

#: 各责任方的修订上限（human 无修订上限，受 MAX_REVIEW_ROUNDS 全局约束）。
REVISION_LIMITS: dict[str, int] = {
    "hydraulic": MAX_HYDRAULIC_REVISIONS,
    "design": MAX_DESIGN_REVISIONS,
    "drawing": MAX_DRAWING_REVISIONS,
}


class RoutingDecision(TypedDict, total=False):
    """路由结论（任务书 §7.1 RoutingDecision 形状）。

    ``next_owner`` 为 ``None`` 表示无回流目标（pass → 人工批准 / 无 owner 可路由）；
    ``error_code`` 仅在 ``blocked=True`` 时出现，取值 ``MULTI_RETRY_EXHAUSTED``。
    """

    next_owner: str | None
    reason: str
    blocked: bool
    exhausted: bool
    error_code: str


def next_owner_for_issues(issues: list[ReviewIssue]) -> str | None:
    """按依赖顺序返回本轮应处理的责任方；无 owner 时返回 None。

    只读 ``issue.owner`` 枚举字段 —— message/object_ref/evidence 等自由文本
    一概不参与（T13：MCP/图纸返回文本不得改变路由）。
    """
    present = {issue.owner for issue in issues}
    for owner in OWNER_PRIORITY:
        if owner in present:
            return owner
    return None


def route_review(
    package: ReviewPackage,
    *,
    revision_counts: dict[str, int],
) -> RoutingDecision:
    """对 ReviewPackage 做一次确定性路由判定。

    Args:
        package: 被路由的审查结论（只读 owner 枚举 / decision / not_checked 长度）。
        revision_counts: 编排器持有的计数器，键为 ``hydraulic / design / drawing /
            review``，值是该方已消耗的修订次数（review 键为已完成的审查轮数）；
            缺省键按 0 处理。

    判定顺序：
    1. ``decision == "pass"`` 且无 issues → next_owner=None（下一跳是批准门）；
    2. ``MAX_REVIEW_ROUNDS`` 耗尽 → blocked + MULTI_RETRY_EXHAUSTED；
    3. 按依赖顺序取第一 owner；该 owner 修订上限耗尽 → blocked + MULTI_RETRY_EXHAUSTED；
    4. 否则返回该 owner（同轮其余 owner 写入 reason 的 deferred 列表）。
    """
    issues = package.issues
    owner = next_owner_for_issues(issues)

    if owner is None and package.decision == "pass" and not package.not_checked:
        return RoutingDecision(
            next_owner=None,
            reason="decision pass with no issues; next hop is approval, not an agent",
            blocked=False,
            exhausted=False,
        )

    if owner is None:
        return RoutingDecision(
            next_owner=None,
            reason=(
                f"no routable owner (decision={package.decision}, "
                f"not_checked={len(package.not_checked)} items pending)"
            ),
            blocked=False,
            exhausted=False,
        )

    review_rounds = revision_counts.get("review", 0)
    if review_rounds >= MAX_REVIEW_ROUNDS:
        return RoutingDecision(
            next_owner=None,
            reason=(
                f"review rounds exhausted ({review_rounds}/{MAX_REVIEW_ROUNDS}); "
                "no further agent calls"
            ),
            blocked=True,
            exhausted=True,
            error_code=MULTI_RETRY_EXHAUSTED,
        )

    if owner in REVISION_LIMITS:
        limit = REVISION_LIMITS[owner]
        used = revision_counts.get(owner, 0)
        if used >= limit:
            return RoutingDecision(
                next_owner=None,
                reason=(
                    f"owner {owner!r} revisions exhausted ({used}/{limit}); "
                    "no further agent calls"
                ),
                blocked=True,
                exhausted=True,
                error_code=MULTI_RETRY_EXHAUSTED,
            )

    owner_issues = [issue for issue in issues if issue.owner == owner]
    blocking_count = sum(1 for issue in owner_issues if issue.severity == "blocking")
    deferred = [
        candidate
        for candidate in OWNER_PRIORITY
        if candidate != owner and any(issue.owner == candidate for issue in issues)
    ]
    if blocking_count:
        reason = f"highest priority owner {owner} with {blocking_count} blocking issues"
    else:
        reason = f"highest priority owner {owner} with {len(owner_issues)} issues"
    if deferred:
        reason += f"; deferred owners: {','.join(deferred)}"
    return RoutingDecision(
        next_owner=owner,
        reason=reason,
        blocked=False,
        exhausted=False,
    )
