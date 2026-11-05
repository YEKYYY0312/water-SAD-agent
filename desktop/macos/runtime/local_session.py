"""Issue an ordinary local-owner session through private desktop IPC only.

There is deliberately no HTTP endpoint and no change to Web authentication.
"""
from __future__ import annotations

import json
from pathlib import Path

try:
    from .paths import DesktopPaths, private_write
except ImportError:
    from paths import DesktopPaths, private_write


def issue_local_session(paths: DesktopPaths) -> Path:
    from water_agent.webapp.auth.models import UserRole
    from water_agent.webapp.auth.session_store import get_session_store
    from water_agent.webapp.auth.storage import configure_database_stores
    from water_agent.webapp.auth.user_store import UserNotFoundError, get_user_store

    database = configure_database_stores()
    if database is None:
        raise ValueError('DESKTOP_LOCAL_OWNER_UNAVAILABLE')
    try:
        try:
            owner = get_user_store().get_by_username('admin')
        except UserNotFoundError:
            raise ValueError('DESKTOP_LOCAL_OWNER_UNAVAILABLE') from None
        if not owner.is_active or owner.role is not UserRole.ADMIN or owner.must_change_password:
            raise ValueError('DESKTOP_LOCAL_OWNER_UNAVAILABLE')
        session = get_session_store().create(owner.user_id)
        destination = paths.credentials / 'desktop-session.json'
        private_write(destination, json.dumps({
            'session_id': session.session_id,
            'csrf_token': session.csrf_token,
            'expires_at': session.expires_at.timestamp(),
        }))
        return destination
    finally:
        database.dispose()
