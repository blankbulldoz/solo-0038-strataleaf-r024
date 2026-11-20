"""Content differences between two read revisions and the commits behind them.

Two related observations live here:

* :meth:`History.diff` compares the documents visible at the ``since`` and ``until``
  read revisions. It is a pure end-point comparison, so a page never depends on
  writes that land after the first page.
* :meth:`History.commits` lists the commits in the same interval that touched a
  collection and/or document key. It explains how the end points were reached,
  including steps that the end-point comparison cannot show.

See ``docs/history.md`` for the relationship between the content and operation
views and for the precise meaning of each change classification.
"""
from dataclasses import dataclass

from .codec import key as checked_key, loads, name as checked_name
from .cursors import decode_cursor, encode_cursor
from .errors import InvalidCursor, InvalidDocument, NotFound, SnapshotTooOld
from .mvcc import Record, checked_revision, latest_row, record

CHANGE_KINDS = ("added", "modified", "deleted", "recreated", "reverted")


@dataclass(frozen=True)
class DocumentChange:
    """One document's net difference plus the lifecycle observed in the interval.

    ``change`` classifies the two end points (see docs/history.md). ``writes`` and
    ``deletes`` count the physical versions inside the interval, so activity hidden
    by an equal end point stays observable: a value written back to its original
    content is ``reverted``; a key deleted and written again is ``recreated`` even
    when the final content equals the initial content; a key added and deleted
    again is ``reverted`` with both end points absent. ``net_changed`` is true when
    the end points differ in content or expiry.
    """
    collection: str
    key: str
    change: str
    before: Record | None
    after: Record | None
    net_changed: bool
    document_changed: bool
    expiry_changed: bool
    writes: int
    deletes: int


@dataclass(frozen=True)
class DiffPage:
    changes: list
    since: int
    until: int
    cursor: str | None


@dataclass(frozen=True)
class CommitEntry:
    """One commit with the operations matched by the requested filters."""
    revision: int
    request_key: str | None
    operations: list
    operation_count: int


@dataclass(frozen=True)
class CommitPage:
    commits: list
    since: int
    until: int
    cursor: str | None


def _page_limit(limit, label):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise InvalidDocument(label + " must be between 1 and 1000")
    return limit


def _revision_pair(store, since, until):
    """Resolve the half-open revision interval, defaulting the start to the floor."""
    floor = int(store.get("floor"))
    if since is None:
        since = floor
    else:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise InvalidDocument("start revision must be a nonnegative integer")
        if since < floor:
            raise SnapshotTooOld(f"start revision {since} is older than retained floor {floor}; "
                                 "pass since=None to cover retained history")
        checked_revision(store, since)
    until = checked_revision(store, until)
    if until < since:
        raise InvalidDocument("start revision must not exceed the end revision")
    return since, until


class History:
    """Browse document differences between revisions and locate their commits."""

    def __init__(self, store):
        self.store = store

    def diff(self, collection, since=None, until=None, limit=100, cursor=None):
        """Return a key-ordered page of documents touched in ``(since, until]``.

        ``since=None`` starts at the retained floor instead of failing on a recycled
        start. Both revisions are fixed for the whole pagination, so commits that
        arrive after the first page cannot change the result set. If retention moves
        the floor past the fixed start, the next page is rejected with
        :class:`SnapshotTooOld` rather than silently returning a different set.
        """
        if not isinstance(collection, str):
            raise InvalidDocument("collection must be a string")
        _page_limit(limit, "diff page size")
        with self.store.read() as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection: " + collection)
            after = None
            if cursor is not None:
                payload = decode_cursor(self.store, cursor)
                if payload.get("kind") != "diff":
                    raise InvalidCursor("cursor does not belong to a history diff")
                if payload.get("collection") != collection:
                    raise InvalidCursor("cursor belongs to a different collection")
                if since is not None and since != payload.get("since"):
                    raise InvalidCursor("cursor fixes a different start revision")
                if until is not None and until != payload.get("until"):
                    raise InvalidCursor("cursor fixes a different end revision")
                since, until = payload.get("since"), payload.get("until")
                after = payload.get("after")
                if not isinstance(after, str):
                    raise InvalidCursor("diff cursor has no key boundary")
            since, until = _revision_pair(self.store, since, until)
            rows = connection.execute(
                "SELECT DISTINCT key FROM versions WHERE collection=? AND rev>? AND rev<=? AND key>? "
                "ORDER BY key LIMIT ?", (collection, since, until, after or "", limit + 1)).fetchall()
            keys = [row[0] for row in rows]
            more = len(keys) > limit
            keys = keys[:limit]
            changes = [self._document_change(connection, collection, document_key, since, until)
                       for document_key in keys]
            continuation = None
            if more:
                continuation = encode_cursor(self.store, {
                    "database": self.store.get("database_id"), "kind": "diff",
                    "collection": collection, "since": since, "until": until, "after": keys[-1]})
            return DiffPage(changes, since, until, continuation)

    @staticmethod
    def _document_change(connection, collection, document_key, since, until):
        rows = connection.execute(
            "SELECT body FROM versions WHERE collection=? AND key=? AND rev>? AND rev<=? ORDER BY rev",
            (collection, document_key, since, until)).fetchall()
        writes = sum(1 for row in rows if row["body"] is not None)
        deletes = len(rows) - writes
        before_row = latest_row(connection, collection, document_key, since)
        after_row = latest_row(connection, collection, document_key, until)
        before, after = record(before_row), record(after_row)
        present_before, present_after = before is not None, after is not None
        if present_before and present_after:
            document_changed = before_row["body"] != after_row["body"]
            expiry_changed = before_row["expires"] != after_row["expires"]
        else:
            document_changed = present_before != present_after
            expiry_changed = present_before != present_after
        net_changed = document_changed or expiry_changed
        if present_after and not present_before:
            kind = "added"
        elif present_after:
            # A tombstone followed by a live write re-creates the key even when the
            # final content happens to equal the content at the start of the interval.
            if deletes:
                kind = "recreated"
            else:
                kind = "modified" if net_changed else "reverted"
        elif present_before:
            kind = "deleted"
        else:
            # Both end points are absent, so the key was created and removed again
            # inside the interval; ``writes``/``deletes`` still record the activity.
            kind = "reverted"
        return DocumentChange(collection, document_key, kind, before, after,
                              net_changed, document_changed, expiry_changed, writes, deletes)

    def commits(self, since=None, until=None, collection=None, key=None, limit=100, cursor=None):
        """Locate commits in ``(since, until]`` that touched a collection and/or key.

        Returned operations are a projection; ``operation_count`` reports the full
        commit size so a filtered view is never mistaken for a partial commit.
        ``since=None`` starts at the retained floor, and a start below the floor is
        rejected with :class:`SnapshotTooOld`.
        """
        _page_limit(limit, "commit page size")
        if collection is not None:
            if not isinstance(collection, str):
                raise InvalidDocument("collection must be a string")
            checked_name(collection)
        if key is not None:
            checked_key(key)
        with self.store.read() as connection:
            after = None
            if cursor is not None:
                payload = decode_cursor(self.store, cursor)
                if payload.get("kind") != "commits":
                    raise InvalidCursor("cursor does not belong to a commit lookup")
                if collection is not None and collection != payload.get("collection"):
                    raise InvalidCursor("cursor belongs to a different collection filter")
                if key is not None and key != payload.get("key"):
                    raise InvalidCursor("cursor belongs to a different document filter")
                if since is not None and since != payload.get("since"):
                    raise InvalidCursor("cursor fixes a different start revision")
                if until is not None and until != payload.get("until"):
                    raise InvalidCursor("cursor fixes a different end revision")
                collection, key = payload.get("collection"), payload.get("key")
                since, until = payload.get("since"), payload.get("until")
                after = payload.get("after")
                if isinstance(after, bool) or not isinstance(after, int):
                    raise InvalidCursor("commit cursor has no revision boundary")
                if collection is not None:
                    checked_name(collection)
                if key is not None:
                    checked_key(key)
            if collection is not None and not connection.execute(
                    "SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection: " + collection)
            since, until = _revision_pair(self.store, since, until)
            boundary = since if after is None else max(since, after)
            entries = []
            more = False
            for row in connection.execute(
                    "SELECT rev,payload,request_key FROM commits WHERE rev>? AND rev<=? ORDER BY rev",
                    (boundary, until)):
                body = loads(row["payload"])
                everything = body.get("operations", [])
                matching = [item for item in everything
                            if (collection is None or item.get("collection") == collection)
                            and (key is None or item.get("key") == key)]
                if not matching:
                    continue
                if len(entries) == limit:
                    more = True
                    break
                entries.append(CommitEntry(row["rev"], row["request_key"],
                                           [{"collection": item.get("collection"), "key": item.get("key"),
                                             "action": "delete" if item.get("document") is None else "put"}
                                            for item in matching],
                                           len(everything)))
            continuation = None
            if more:
                continuation = encode_cursor(self.store, {
                    "database": self.store.get("database_id"), "kind": "commits",
                    "collection": collection, "key": key, "since": since, "until": until,
                    "after": entries[-1].revision})
            return CommitPage(entries, since, until, continuation)
