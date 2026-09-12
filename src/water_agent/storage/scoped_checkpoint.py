"""Server-authorized wrapper for Web LangGraph checkpoint access."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from water_agent.storage.repository import WebSessionRepository


class ScopedPostgresSaver(BaseCheckpointSaver):
    """Bind a saver to one authenticated user and re-authorize every call.

    The wrapper makes ``thread_id`` a locator only.  Knowing another user's ID
    cannot reach the underlying checkpointer because the corresponding
    ``web_sessions`` row must be visible to the bound user first.
    """

    def __init__(
        self,
        saver: BaseCheckpointSaver,
        sessions: WebSessionRepository,
        user_id: UUID,
    ) -> None:
        super().__init__(serde=saver.serde)
        self.saver = saver
        self.sessions = sessions
        self.user_id = user_id

    def _authorize_config(self, config: RunnableConfig) -> None:
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        self.sessions.get_for_user(thread_id, self.user_id)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._authorize_config(config)
        return self.saver.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            raise ValueError("scoped checkpoint listing requires thread_id")
        self._authorize_config(config)
        yield from self.saver.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._authorize_config(config)
        return self.saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._authorize_config(config)
        self.saver.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self.sessions.get_for_user(thread_id, self.user_id)
        self.saver.delete_thread(thread_id)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        await asyncio.to_thread(self._authorize_config, config)
        return await asyncio.to_thread(self.saver.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise ValueError("scoped checkpoint listing requires thread_id")
        await asyncio.to_thread(self._authorize_config, config)
        items = await asyncio.to_thread(
            lambda: list(self.saver.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        await asyncio.to_thread(self._authorize_config, config)
        return await asyncio.to_thread(
            self.saver.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self._authorize_config, config)
        await asyncio.to_thread(
            self.saver.put_writes, config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.sessions.get_for_user, thread_id, self.user_id)
        await asyncio.to_thread(self.saver.delete_thread, thread_id)


__all__ = ["ScopedPostgresSaver"]
