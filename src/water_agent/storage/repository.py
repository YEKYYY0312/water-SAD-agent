"""Owner/membership-filtered Web repositories.

Every lookup takes the authenticated user ID as a required argument.  Resource
IDs are only selectors; they never grant access on their own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, or_, select

from water_agent.storage.database import Database
from water_agent.storage.models import (
    FeedbackCandidateRow,
    Project,
    ProjectMembership,
    Run,
    WebSession,
)

#: Project roles allowed to write into a project (same set as the Web
#: ``_can_write_project`` check and the memory write clause).
_PROJECT_WRITE_ROLES = ("owner", "member")


class ResourceNotFoundError(LookupError):
    """Resource is absent or outside the caller's authorization scope."""


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, owner_user_id: UUID, name: str) -> Project:
        project = Project(
            project_id=str(uuid4()),
            owner_user_id=str(owner_user_id),
            name=name,
            created_at=_now(),
            updated_at=_now(),
        )
        with self.database.transaction() as session:
            session.add(project)
            session.add(
                ProjectMembership(
                    project_id=project.project_id,
                    user_id=project.owner_user_id,
                    role="owner",
                    created_at=_now(),
                )
            )
        return project

    def get_for_user(self, project_id: UUID | str, user_id: UUID | str) -> Project:
        project_key = str(project_id)
        user_key = str(user_id)
        with self.database.transaction() as session:
            query = select(Project).where(
                Project.project_id == project_key,
                exists(
                    select(ProjectMembership.project_id).where(
                        ProjectMembership.project_id == Project.project_id,
                        ProjectMembership.user_id == user_key,
                    )
                ),
            )
            project = session.execute(query).scalar_one_or_none()
            if project is None:
                raise ResourceNotFoundError("project not found")
            return project

    def list_for_user(self, user_id: UUID | str) -> list[tuple[Project, str]]:
        """List only projects where the authenticated user has membership."""

        with self.database.transaction() as session:
            rows = session.execute(
                select(Project, ProjectMembership.role)
                .join(ProjectMembership, ProjectMembership.project_id == Project.project_id)
                .where(ProjectMembership.user_id == str(user_id))
                .order_by(Project.updated_at.desc(), Project.project_id.asc())
            )
            return [(project, str(role)) for project, role in rows]

    def membership_role(self, project_id: UUID | str, user_id: UUID | str) -> str:
        """Return the server-recorded role or hide the project as not found."""

        with self.database.transaction() as session:
            role = session.execute(
                select(ProjectMembership.role).where(
                    ProjectMembership.project_id == str(project_id),
                    ProjectMembership.user_id == str(user_id),
                )
            ).scalar_one_or_none()
            if role is None:
                raise ResourceNotFoundError("project not found")
            return str(role)

    def add_member(self, project_id: UUID | str, actor_user_id: UUID | str, user_id: UUID | str, role: str = "member") -> None:
        with self.database.transaction() as session:
            project = session.execute(
                select(Project).where(
                    Project.project_id == str(project_id),
                    Project.owner_user_id == str(actor_user_id),
                    exists(
                        select(ProjectMembership.project_id).where(
                            ProjectMembership.project_id == Project.project_id,
                            ProjectMembership.user_id == str(actor_user_id),
                        )
                    ),
                )
            ).scalar_one_or_none()
            if project is None:
                raise ResourceNotFoundError("project not found")
            session.merge(
                ProjectMembership(
                    project_id=str(project_id), user_id=str(user_id), role=role, created_at=_now()
                )
            )


class WebSessionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, owner_user_id: UUID | str, *, project_id: UUID | str | None = None, title: str = "") -> WebSession:
        now = _now()
        record = WebSession(
            thread_id=str(uuid4()),
            owner_user_id=str(owner_user_id),
            project_id=str(project_id) if project_id is not None else None,
            title=title,
            created_at=now,
            updated_at=now,
        )
        with self.database.transaction() as session:
            if record.project_id is not None:
                membership = session.execute(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == record.project_id,
                        ProjectMembership.user_id == record.owner_user_id,
                    )
                ).scalar_one_or_none()
                if membership is None:
                    raise ResourceNotFoundError("project not found")
            session.add(record)
        return record

    def get_for_user(self, thread_id: UUID | str, user_id: UUID | str) -> WebSession:
        with self.database.transaction() as session:
            record = session.execute(
                select(WebSession).where(
                    WebSession.thread_id == str(thread_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                            )
                        ),
                    ),
                )
            ).scalar_one_or_none()
            if record is None:
                raise ResourceNotFoundError("session not found")
            return record

    def list_for_user(
        self,
        user_id: UUID | str,
        *,
        project_id: UUID | str | None = None,
        personal_only: bool = False,
    ) -> list[WebSession]:
        """List sessions after applying owner/membership filtering in SQL."""

        user_key = str(user_id)
        access = or_(
            (WebSession.project_id.is_(None) & (WebSession.owner_user_id == user_key)),
            exists(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.project_id == WebSession.project_id,
                    ProjectMembership.user_id == user_key,
                )
            ),
        )
        with self.database.transaction() as session:
            query = select(WebSession).where(access)
            if project_id is not None:
                query = query.where(WebSession.project_id == str(project_id))
            elif personal_only:
                query = query.where(WebSession.project_id.is_(None))
            return list(
                session.execute(
                    query.order_by(WebSession.updated_at.desc(), WebSession.thread_id.asc())
                ).scalars()
            )

    def set_model(self, thread_id: UUID | str, user_id: UUID | str, model: str | None) -> WebSession:
        """改会话默认模型（P7-S15；``None`` = 清除，回到全局模型）。

        与建 run 同一条写权限规则：个人会话只有拥有者；项目会话要写角色成员（项目 viewer 当作不存在）。
        模型 id 是否在目录里、是否已验证由调用方（路由）校验，这里只落库。不改 ``updated_at``——换默认模型
        不应让会话在列表里跳到最前。
        """

        with self.database.transaction() as session:
            record = session.execute(
                select(WebSession).where(
                    WebSession.thread_id == str(thread_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                                ProjectMembership.role.in_(_PROJECT_WRITE_ROLES),
                            )
                        ),
                    ),
                )
            ).scalar_one_or_none()
            if record is None:
                raise ResourceNotFoundError("session not found")
            record.model = model
            return record

    def delete_for_user(self, thread_id: UUID | str, user_id: UUID | str) -> None:
        with self.database.transaction() as session:
            found = session.execute(
                select(WebSession.thread_id).where(
                    WebSession.thread_id == str(thread_id),
                    WebSession.owner_user_id == str(user_id),
                    or_(
                        WebSession.project_id.is_(None),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                            )
                        ),
                    ),
                )
            ).first()
            if found is None:
                raise ResourceNotFoundError("session not found")
            session.execute(delete(WebSession).where(WebSession.thread_id == str(thread_id)))


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self, thread_id: UUID | str, owner_user_id: UUID | str, *, model: str | None = None
    ) -> Run:
        """Create a queued run; in a project session only a writing member may.

        ``model``（P7-S15）：这次运行**实际选用**的模型 id（消息 > 会话默认的结果；``None`` = 全局模型）。
        落库是为了进程重启后挂起的那一轮能恢复到当时选的模型，而不是静默换回默认。

        Membership alone makes a session readable, but a run writes the run row
        and the session's checkpoint, so a project ``viewer`` is answered as if
        the session did not exist (the Web route refuses it with 403 first).
        """

        now = _now()
        with self.database.transaction() as session:
            web_session = session.execute(
                select(WebSession).where(
                    WebSession.thread_id == str(thread_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(owner_user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(owner_user_id),
                                ProjectMembership.role.in_(_PROJECT_WRITE_ROLES),
                            )
                        ),
                    ),
                )
            ).scalar_one_or_none()
            if web_session is None:
                raise ResourceNotFoundError("session not found")
            record = Run(
                run_id=str(uuid4()),
                thread_id=str(thread_id),
                owner_user_id=str(owner_user_id),
                status="queued",
                model=model,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            return record

    def get_for_user(self, run_id: UUID | str, user_id: UUID | str) -> Run:
        with self.database.transaction() as session:
            record = session.execute(
                select(Run)
                .join(WebSession, WebSession.thread_id == Run.thread_id)
                .where(
                    Run.run_id == str(run_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                            )
                        ),
                    ),
                )
            ).scalar_one_or_none()
            if record is None:
                raise ResourceNotFoundError("run not found")
            return record

    def get_owned_for_user(self, run_id: UUID | str, user_id: UUID | str) -> Run:
        """Return a run only when the caller is its server-recorded owner.

        Project membership makes a run readable to project members, but
        cancellation is an owner action.  The owner check is kept in the
        repository so a URL id cannot be mistaken for authorization.
        """

        with self.database.transaction() as session:
            record = session.execute(
                select(Run)
                .join(WebSession, WebSession.thread_id == Run.thread_id)
                .where(
                    Run.run_id == str(run_id),
                    Run.owner_user_id == str(user_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                            )
                        ),
                    ),
                )
            ).scalar_one_or_none()
            if record is None:
                raise ResourceNotFoundError("run not found")
            return record

    def list_for_session(self, thread_id: UUID | str, user_id: UUID | str) -> list[Run]:
        """List a session's runs only when the caller can read that session."""

        with self.database.transaction() as session:
            query = (
                select(Run)
                .join(WebSession, WebSession.thread_id == Run.thread_id)
                .where(
                    Run.thread_id == str(thread_id),
                    or_(
                        (
                            (WebSession.project_id.is_(None))
                            & (WebSession.owner_user_id == str(user_id))
                        ),
                        exists(
                            select(ProjectMembership.project_id).where(
                                ProjectMembership.project_id == WebSession.project_id,
                                ProjectMembership.user_id == str(user_id),
                            )
                        ),
                    ),
                )
                .order_by(Run.created_at.desc(), Run.run_id.asc())
            )
            return list(session.execute(query).scalars())

    def set_status(
        self,
        run_id: UUID | str,
        owner_user_id: UUID | str,
        status: str,
        *,
        expected_statuses: tuple[str, ...] = (),
    ) -> Run:
        """Atomically update a run state owned by the caller.

        ``expected_statuses`` makes the transition compare-and-set shaped;
        the Web run manager still serializes its own events, while the
        database remains the authoritative owner/status boundary.
        """

        # waiting_confirmation（P7-S7）：作答停在人工确认门，不是终态；决定后经 queued 在同一个 run 上
        # 继续。状态只在这里校验（runs.status 没有数据库约束），新增取值不需要迁移。
        allowed = {
            "queued",
            "running",
            "succeeded",
            "failed",
            "blocked",
            "cancelled",
            "waiting_confirmation",
        }
        if status not in allowed:
            raise ValueError(f"unknown run status: {status}")
        with self.database.transaction() as session:
            query = select(Run).where(
                Run.run_id == str(run_id),
                Run.owner_user_id == str(owner_user_id),
            )
            if expected_statuses:
                query = query.where(Run.status.in_(expected_statuses))
            record = session.execute(query).scalar_one_or_none()
            if record is None:
                raise ResourceNotFoundError("run not found")
            record.status = status
            record.updated_at = _now()
            return record


class FeedbackCandidateRepository:
    """Persist review-only feedback candidates, never evaluation gold."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_or_get(
        self,
        *,
        run_id: UUID | str,
        thread_id: UUID | str,
        user_id: UUID | str,
        rating: str,
        comment: str = "",
    ) -> tuple[FeedbackCandidateRow, bool]:
        with self.database.transaction() as session:
            existing = session.execute(
                select(FeedbackCandidateRow).where(
                    FeedbackCandidateRow.run_id == str(run_id),
                    FeedbackCandidateRow.user_id == str(user_id),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
            record = FeedbackCandidateRow(
                feedback_id=str(uuid4()),
                run_id=str(run_id),
                thread_id=str(thread_id),
                user_id=str(user_id),
                rating=rating,
                comment=comment,
                status="candidate",
                created_at=_now(),
            )
            session.add(record)
            return record, True


__all__ = [
    "FeedbackCandidateRepository",
    "ProjectRepository",
    "ResourceNotFoundError",
    "RunRepository",
    "WebSessionRepository",
]
