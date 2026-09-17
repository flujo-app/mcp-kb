"""The refresh policy: when a source is looked at again, and what the answer is worth.

``KnowledgeBase`` owns the transaction -- the lock, the records, the one
assignment that swaps the snapshot -- and this module owns the decisions it
makes along the way: which sources are due, whether one has moved since its
record was built, whether a failed rebuild is worth serving over the harvest
already on disk, and whether the result counts as a change at all.

The background loop lives here too, because all it does per tick is apply that
policy; what to do with what it finds is still the server's. It drives the
knowledge base through the same public surface ``routes.py`` uses, so nothing
here reaches into how a snapshot is built or stored.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Source, redact
from ..sources import SourceError, fingerprint
from .index import SourceRecord
from .snapshot import SERVABLE, stale

if TYPE_CHECKING:
    from ..server import KnowledgeBase

log = logging.getLogger(__name__)

# How often the background loop wakes to ask what is due. A source's own
# ``refresh`` decides when it is actually rebuilt; this only bounds how late
# that can be, and a short tick costs nothing because a tick with nothing due
# does no work at all.
TICK_SECONDS = 5


class Schedule:
    """When each source was last examined, and which are due to be again.

    Keyed by monotonic time rather than by a record's ``built``: ``built`` only
    moves when a rebuild actually replaces a record, so scheduling off it brings
    an unchanged -- or persistently failing -- source due on every tick forever.
    """

    def __init__(self) -> None:
        self._checked: dict[str, float] = {}

    def examined(self, name: str, at: float) -> None:
        """Note that ``name`` was looked at, whether or not it was rebuilt."""
        self._checked[name] = at

    def due(self, sources: Iterable[Source]) -> list[str]:
        """The sources whose own refresh interval has elapsed since the last look.

        A source never checked (``-inf``) is due immediately.
        """
        now = time.monotonic()
        return [
            source.name
            for source in sources
            if source.refresh_seconds is not None
            and now - self._checked.get(source.name, float("-inf"))
            >= source.refresh_seconds
        ]


def tick_seconds(shortest_refresh: int | None) -> float:
    """How long the loop sleeps: never longer than the shortest ``refresh:``.

    ``shortest_refresh`` is ``config.min_refresh_seconds``, read once by the
    caller -- the loop's own ``while`` condition already has it, so this takes
    the value rather than the config, and nothing reads it a second time. A
    fixed ``TICK_SECONDS`` tick can be late by almost a whole tick, which
    matters once a source asks for something shorter than that; ``None`` (no
    source scheduled) sleeps the full tick, since nothing is due to be late.
    """
    if shortest_refresh is None:
        return TICK_SECONDS
    return min(TICK_SECONDS, shortest_refresh)


def moved(source: Source, cache: Path, record: SourceRecord) -> bool:
    """Whether ``source`` has moved on since ``record`` was built.

    Anything not ``"ok"`` is due unconditionally, a record already marked stale
    included. Its fingerprint is the last good one, so a source that came back
    without changing would still match it and would go on being reported stale
    forever; only an actual rebuild can clear that.
    """
    if record.status != "ok" or record.root is None:
        return True
    root = Path(record.root)
    if not root.is_dir():
        return True
    try:
        return fingerprint(source, cache, root) != record.fingerprint
    except SourceError:
        return True


def keep_last_good(old: SourceRecord | None, new: SourceRecord) -> SourceRecord:
    """``new``, unless it is a failure over a harvest worth going on serving.

    A refresh reaching a source is a second chance to fail, and a remote that
    is momentarily unreachable -- a git remote most of all -- must not empty a
    catalogue that was complete a minute ago. So a failed *rebuild* over an
    existing record becomes that record, marked stale and carrying the error,
    and the skills keep being served from the tree already on disk.

    Only the cold start, which has no earlier record, treats a failure as a
    failed source. The tree is checked because rows naming a directory that is
    gone would serve nothing: at that point the failure is the better answer.
    """
    if new.status != "failed" or old is None or old.status not in SERVABLE:
        return new
    if old.root is None or not Path(old.root).is_dir():
        return new
    return stale(old, new.error or "refresh failed")


def same_failure(old: SourceRecord | None, new: SourceRecord) -> bool:
    """Whether a rebuild produced the same failure the record already carried.

    A source that is still missing has not *changed*, and counting it as a
    rebuild would advance the generation on every single pass -- announcing a
    new catalogue to every client, forever, because one directory is absent.
    The same holds for a source that is still stale for the same reason.
    """
    return (
        old is not None
        and new.status in ("failed", "stale")
        and old.status == new.status
        and old.error == new.error
    )


async def loop(knowledge_base: KnowledgeBase) -> None:
    """One verification pass, then a pass per tick for whatever is due.

    The first pass is the other half of the cold start: boot trusted the index
    without checking a fingerprint, and this is where that check happens. After
    it, a source is re-examined only once its own ``refresh`` interval has
    elapsed, and a config that declares no interval anywhere stops here --
    nothing asked to be watched.
    """
    await _pass(knowledge_base)
    while (shortest := knowledge_base.config.min_refresh_seconds) is not None:
        await asyncio.sleep(tick_seconds(shortest))
        due = knowledge_base.schedule.due(knowledge_base.config.sources)
        if due:
            await _pass(knowledge_base, only=due)


async def _pass(knowledge_base: KnowledgeBase, **kwargs) -> None:
    """One refresh, whose failure is logged and never ends the loop.

    A background task that dies takes the whole refresh with it and the server
    goes on serving a frozen catalogue while looking healthy. That silence is
    the failure mode this exists to prevent, so every exception is caught here
    and the loop goes round again.
    """
    try:
        rebuilt = await knowledge_base.refresh_async(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the loop must outlive any failure
        trace = "".join(traceback.format_exception(exc))
        secrets = knowledge_base.config.secrets()
        log.error("refresh pass failed\n%s", redact(trace, secrets))
    else:
        if rebuilt:
            log.info(
                "rebuilt %s; now at generation %d",
                ", ".join(rebuilt),
                knowledge_base.generation,
            )
