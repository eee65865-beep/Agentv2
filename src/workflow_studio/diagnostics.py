"""Bounded technical logs without raw exceptions, prompts, credentials or locals."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import traceback
from uuid import UUID

logger = logging.getLogger("workflow_studio.diagnostics")


def log_failure(operation: str, error: Exception, *, run_id: str | None = None, agent: str = "workflow") -> None:
    try:
        safe_id = str(UUID(run_id)) if run_id else "unavailable"
    except (ValueError, TypeError):
        safe_id = "unavailable"
    allowed_agents = {"workflow", "storage", "requirements_agent", "design_agent", "coding_agent", "review_agent"}
    frames = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(error.__traceback__)[-8:])
    logger.error("operation=%s run_id=%s agent=%s error_type=%s errno=%s frames=%s", operation, safe_id, agent if agent in allowed_agents else "unknown", type(error).__name__, getattr(error, "errno", None), frames or "unavailable")


def configure_logging(log_dir: Path | None = None) -> None:
    """Only our namespace is written; rerenders do not add duplicate handlers."""
    directory = log_dir or Path(__file__).resolve().parents[2] / "logs"
    target = directory / "workflow_studio.log"
    namespace = logging.getLogger("workflow_studio")
    if any(getattr(handler, "_studio_file", None) == str(target) for handler in namespace.handlers):
        return
    try:
        for path in [directory, target, directory / "workflow_studio.log.1", directory / "workflow_studio.log.2"]:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise OSError("linked_log_path")
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
        handler._studio_file = str(target)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        namespace.addHandler(handler)
        namespace.setLevel(logging.INFO)
    except OSError as error:
        log_failure("configure_logging", error)
