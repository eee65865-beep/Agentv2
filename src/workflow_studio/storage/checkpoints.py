"""Local SQLite LangGraph saver and durable session journal, no new dependency."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import RLock

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP, get_checkpoint_metadata
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from workflow_studio.storage.paths import APPLICATION_ROOT, canonical_run_id, safe_child, safe_root


class CheckpointError(ValueError):
    """Safe persistence/version error; no private payload in messages."""


class CheckpointConflictError(CheckpointError):
    pass


ALLOWED_TYPES = [
    ("langgraph.types", "Interrupt"),
    ("workflow_studio.state.workflow", "WorkflowEvent"),
    ("workflow_studio.state.workflow", "WorkflowStatus"),
    ("workflow_studio.state.review", "ReviewRecord"),
    ("workflow_studio.state.review", "ReviewOutput"),
    ("workflow_studio.state.review", "ReviewStatus"),
    ("workflow_studio.state.coding", "CodingOutput"),
    ("workflow_studio.state.coding", "CodeFile"),
    ("workflow_studio.state.patch", "RepairReceipt"),
    ("workflow_studio.state.patch", "ChangeSummary"),
]


class CheckpointStore(BaseCheckpointSaver):
    """Full checkpoints plus pending writes for this project's scalar graph."""

    def __init__(self, root: Path | None = None):
        json_types = [tuple(module.split(".")) + (name,) for module, name in ALLOWED_TYPES]
        super().__init__(serde=JsonPlusSerializer(pickle_fallback=False, allowed_json_modules=json_types, allowed_msgpack_modules=ALLOWED_TYPES))
        self.root = safe_root(root if root is not None else APPLICATION_ROOT / "checkpoints")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = safe_child(self.root, "workflows.sqlite3")
        for name in ("workflows.sqlite3-wal", "workflows.sqlite3-shm", "workflows.sqlite3-journal"):
            safe_child(self.root, name)
        self.lock = RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread TEXT, ns TEXT, id TEXT, parent TEXT, payload BLOB, metadata BLOB,
                PRIMARY KEY(thread,ns,id));
            CREATE TABLE IF NOT EXISTS writes (
                thread TEXT, ns TEXT, checkpoint TEXT, task TEXT, idx INTEGER, channel TEXT, payload BLOB,
                PRIMARY KEY(thread,ns,checkpoint,task,idx));
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def _encode(self, value):
        kind, body = self.serde.dumps_typed(value)
        if len(body) > 8 * 1024 * 1024:
            raise CheckpointError("checkpoint_payload_too_large")
        return kind.encode("ascii") + b"\0" + body

    def _decode(self, data):
        if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024 + 16 or b"\0" not in data:
            raise CheckpointError("invalid_checkpoint_payload")
        kind, body = data.split(b"\0", 1)
        if len(body) > 8 * 1024 * 1024:
            raise CheckpointError("checkpoint_payload_too_large")
        if kind not in (b"msgpack", b"json", b"bytes", b"null"):
            raise CheckpointError("unsupported_checkpoint_encoding")
        try:
            return self.serde.loads_typed((kind.decode("ascii"), body))
        except Exception:
            raise CheckpointError("cannot_decode_checkpoint") from None

    def get_tuple(self, config):
        c = config["configurable"]
        thread, ns = c["thread_id"], c.get("checkpoint_ns", "")
        with self.lock:
            if c.get("checkpoint_id"):
                row = self.db.execute("SELECT id,parent,payload,metadata FROM checkpoints WHERE thread=? AND ns=? AND id=?", (thread, ns, c["checkpoint_id"])).fetchone()
            else:
                row = self.db.execute("SELECT id,parent,payload,metadata FROM checkpoints WHERE thread=? AND ns=? ORDER BY id DESC LIMIT 1", (thread, ns)).fetchone()
            if row is None:
                return None
            identifier, parent, payload, metadata = row
            writes = self.db.execute("SELECT task,channel,payload FROM writes WHERE thread=? AND ns=? AND checkpoint=? ORDER BY task,idx", (thread, ns, identifier)).fetchall()
        make_config = lambda key: {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": key}}
        return CheckpointTuple(make_config(identifier), self._decode(payload), self._decode(metadata), make_config(parent) if parent else None,
                               [(task, channel, self._decode(body)) for task, channel, body in writes])

    def put(self, config, checkpoint, metadata, new_versions):
        c = config["configurable"]
        thread, ns = c["thread_id"], c.get("checkpoint_ns", "")
        payload = self._encode(checkpoint)
        meta = self._encode(get_checkpoint_metadata(config, metadata))
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?)", (thread, ns, checkpoint["id"], c.get("checkpoint_id"), payload, meta))
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        c = config["configurable"]
        with self.lock, self.db:
            for index, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, index)
                # Never deserialize exception constructors or provider response bodies.
                if channel == "__error__":
                    value = type(value).__name__
                verb = "INSERT OR REPLACE" if idx < 0 else "INSERT OR IGNORE"
                self.db.execute(f"{verb} INTO writes VALUES (?,?,?,?,?,?,?)", (c["thread_id"], c.get("checkpoint_ns", ""), c["checkpoint_id"], task_id, idx, channel, self._encode(value)))

    def list(self, config, *, filter=None, before=None, limit=None):
        with self.lock:
            rows = self.db.execute("SELECT thread,ns,id FROM checkpoints ORDER BY id DESC").fetchall()
        for thread, ns, identifier in rows:
            if config is not None and (thread != config["configurable"]["thread_id"] or ns != config["configurable"].get("checkpoint_ns", "")):
                continue
            if before and identifier >= before["configurable"]["checkpoint_id"]:
                continue
            item = self.get_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": identifier}})
            if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                continue
            if limit is not None:
                if limit <= 0:
                    return
                limit -= 1
            yield item

    @contextmanager
    def claim(self, session_id):
        """OS lock survives app refresh and is released by the OS after a crash."""
        path = safe_child(self.root, canonical_run_id(session_id) + ".lock")
        with path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                import os
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                raise CheckpointConflictError("session_already_running") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def default_checkpoints():
    return CheckpointStore()
