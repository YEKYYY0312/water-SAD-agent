"""Owner/membership-filtered Web repositories.

Every lookup takes the authenticated user ID as a required argument.  Resource
IDs are only selectors; they never grant access on their own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, or_, select

from water_agent.storage.database import Database
from water_agent.storage.models import Project, ProjectMembership, Run, WebSession


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

    def create(self, thread_id: UUID | str, owner_user_id: UUID | str) -> Run:
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

        allowed = {"queued", "running", "succeeded", "failed", "blocked", "cancelled"}
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


__all__ = ["ProjectRepository", "ResourceNotFoundError", "RunRepository", "WebSessionRepository"]
