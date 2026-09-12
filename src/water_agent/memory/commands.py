"""Explicit command adapters for P5 memory candidate creation."""

from __future__ import annotations

import re

from water_agent.memory.write_contracts import RememberCommand

_REMEMBER_COMMAND = re.compile(
    r"^\s*/remember(?:\s+(?P<content>.*))?\s*$", re.IGNORECASE | re.DOTALL
)


def adapt_remember_command(message: str) -> RememberCommand | None:
    """Parse `/remember` into a confirmation-form seed without writing anything."""

    match = _REMEMBER_COMMAND.match(message)
    if match is None:
        return None
    content = (match.group("content") or "").strip()
    if not content:
        raise ValueError("/remember requires content before the confirmation form can open")
    return RememberCommand(content=content)


__all__ = ["adapt_remember_command"]
