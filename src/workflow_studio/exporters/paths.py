"""Pure path validation for generated files; no filesystem writes or execution."""

import re
import unicodedata
from pathlib import PureWindowsPath

from workflow_studio.errors import UnsafeCodePathError

PROTECTED_PARTS = {".git", ".hg", ".svn", ".ssh", ".aws", ".codex", ".agents", ".github", ".venv", "venv", "__pycache__"}


def validate_export_path(path: str) -> str:
    """Reject traversal and ambiguous Windows/POSIX targets before any export.

    Accept forward-slash relative paths only. A future disk exporter must also
    check resolved containment, existing files and symlinks at write time.
    """
    if not isinstance(path, str) or not path or len(path) > 240 or path != path.strip():
        raise UnsafeCodePathError("path_must_be_nonempty_without_surrounding_whitespace")
    if path != unicodedata.normalize("NFKC", path):
        raise UnsafeCodePathError("path_must_use_unambiguous_unicode")
    if path.startswith("/") or PureWindowsPath(path).drive or "\\" in path:
        raise UnsafeCodePathError("path_must_be_relative_with_forward_slashes")
    if any(char in '<>:"|?*%' or unicodedata.category(char).startswith("C") for char in path):
        raise UnsafeCodePathError("path_contains_unsafe_characters")
    parts = path.split("/")
    for part in parts:
        name = part.casefold()
        if part in ("", ".", "..") or part.endswith((".", " ")):
            raise UnsafeCodePathError("path_contains_traversal_or_ambiguous_component")
        if name in PROTECTED_PARTS or name == ".env" or (name.startswith(".env.") and name != ".env.example") or name == "secrets.toml":
            raise UnsafeCodePathError("path_targets_protected_configuration")
        if re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", name.split(".", 1)[0].rstrip(" .")):
            raise UnsafeCodePathError("path_uses_reserved_device_name")
    return path
