"""Guard UUID directories, source roots and Windows/POSIX links."""

import os
from pathlib import Path
from uuid import UUID

from workflow_studio.errors import UnsafeStoragePathError

APPLICATION_ROOT = Path(__file__).resolve().parents[3]


def canonical_run_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("invalid_uuid")
    except (ValueError, TypeError, AttributeError):
        raise UnsafeStoragePathError("invalid_run_id") from None
    return value


def guard_no_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise UnsafeStoragePathError("linked_storage_path")
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & 0x400:
            raise UnsafeStoragePathError("reparse_storage_path")


def safe_root(root: Path) -> Path:
    path = Path(os.path.abspath(root))
    guard_no_links(path)
    protected = [APPLICATION_ROOT, *(APPLICATION_ROOT / name for name in ("src", "tests", ".git", ".venv", ".agents", ".codex", ".aws", ".streamlit"))]
    if path == APPLICATION_ROOT or any(path.is_relative_to(item) for item in protected[1:]):
        raise UnsafeStoragePathError("application_source_cannot_be_output_root")
    return path


def safe_child(root: Path, name: str) -> Path:
    path = root / name
    guard_no_links(path)
    if path.resolve().parent != root.resolve():
        raise UnsafeStoragePathError("storage_path_escaped_root")
    return path
