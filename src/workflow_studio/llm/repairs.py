"""Context-local successful patch receipts, mirroring model-call collection."""

from contextlib import contextmanager
from contextvars import ContextVar

_receipts = ContextVar("workflow_patch_receipts", default=None)


@contextmanager
def collect_repairs():
    records = []
    token = _receipts.set(records)
    try:
        yield records
    finally:
        _receipts.reset(token)


def record_repair(receipt):
    records = _receipts.get()
    if records is not None:
        records.append(receipt)
