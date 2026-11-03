"""Delete old audio/video chat attachments from agent ``inbound/`` folders.

Uploaded videos are large and only needed while the agent works on them, so
they are removed ``inbound_media_retention_days`` after upload. Age comes from
the ``{unix_ts}_`` prefix every stored inbound name carries; images and
documents stay because chat history renders them.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from octop.infra.gateway.media.constants import INBOUND_DIR
from octop.infra.gateway.media.inbound_store import (
    _STORED_TS_PREFIX,
    INBOUND_EXTENSION_MEDIA_TYPES,
    _is_av_media_type,
)

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60 * 60
_DAY_SECONDS = 24 * 60 * 60


def _is_expired_media(name: str, *, cutoff: float) -> bool:
    match = _STORED_TS_PREFIX.match(name)
    if match is None or int(match.group(1)) >= cutoff:
        return False
    ext = PurePosixPath(name).suffix.lower()
    media_type = INBOUND_EXTENSION_MEDIA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or ""
    return _is_av_media_type(media_type.lower())


# IM attachments sit under ``inbound/{channel_type}/{channel_id}/``.
_MAX_DEPTH = 3


async def sweep_inbound_media(
    workspace: BackendWorkspace, *, retention_days: int, now: float | None = None
) -> list[str]:
    """Delete expired audio/video files under *workspace* ``inbound/``; return their paths."""
    cutoff = (time.time() if now is None else now) - retention_days * _DAY_SECONDS
    removed: list[str] = []
    pending = [(INBOUND_DIR, 0)]
    while pending:
        folder, depth = pending.pop()
        listing = await workspace.als(folder)
        if listing is None:
            continue
        for entry in listing.entries or []:
            name = PurePosixPath(str(entry.get("path") or "")).name
            if not name:
                continue
            rel = f"{folder}/{name}"
            if entry.get("is_dir"):
                if depth < _MAX_DEPTH:
                    pending.append((rel, depth + 1))
            elif _is_expired_media(name, cutoff=cutoff):
                await workspace.adelete(rel)
                removed.append(rel)
    return removed


async def sweep_inbound_media_forever(
    workspaces: Callable[[], Iterable[tuple[str, BackendWorkspace]]],
    *,
    retention_days: int,
    interval_seconds: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every running agent's ``inbound/`` at start, then every hour."""
    while True:
        try:
            # Listing agents reads the database; keep it off the event loop.
            targets = list(await asyncio.to_thread(workspaces))
        except Exception:
            logger.warning("inbound retention: listing agents failed", exc_info=True)
            targets = []
        for agent_id, workspace in targets:
            try:
                for path in await sweep_inbound_media(workspace, retention_days=retention_days):
                    logger.info("inbound retention: deleted %s for agent %s", path, agent_id)
            except Exception:
                logger.warning(
                    "inbound retention sweep failed for agent %s", agent_id, exc_info=True
                )
        await asyncio.sleep(interval_seconds)
