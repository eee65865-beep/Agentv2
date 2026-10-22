"""Independent immutable evidence files; terminal RunStore files stay untouched."""

import os
from pathlib import Path
from uuid import uuid4

from workflow_studio.storage.paths import APPLICATION_ROOT, canonical_run_id, safe_child, safe_root
from workflow_studio.validation.models import ValidationReport


class ValidationStore:
    def __init__(self, root=None):
        self.root = safe_root(root if root is not None else APPLICATION_ROOT / "artifacts" / "validation")

    def save(self, report):
        report = ValidationReport.model_validate(report.model_dump())
        payload = report.model_dump_json(indent=2).encode("utf-8")
        if len(payload) > 131072:
            raise ValueError("validation_report_too_large")
        self.root.mkdir(parents=True, exist_ok=True)
        folder = safe_child(self.root, canonical_run_id(report.run_id))
        folder.mkdir(exist_ok=True)
        path = safe_child(folder, canonical_run_id(report.report_id) + ".json")
        pending = safe_child(folder, ".pending-" + uuid4().hex)
        try:
            with pending.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # link is an atomic create-if-absent publish; no existing report overwrite.
            try:
                os.link(pending, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError("validation_report_conflict") from None
        finally:
            pending.unlink(missing_ok=True)
        return path

    def load(self, run_id, report_id):
        folder = safe_child(self.root, canonical_run_id(run_id))
        path = safe_child(folder, canonical_run_id(report_id) + ".json")
        if path.stat().st_size > 131072:
            raise ValueError("validation_report_too_large")
        report = ValidationReport.model_validate_json(path.read_bytes())
        if report.run_id != run_id or report.report_id != report_id:
            raise ValueError("validation_report_identity_mismatch")
        return report

    def list(self, run_id):
        folder = safe_child(self.root, canonical_run_id(run_id))
        if not folder.exists():
            return []
        values = []
        for path in sorted(folder.glob("*.json"), reverse=True):
            try:
                values.append(self.load(run_id, path.stem))
            except (ValueError, OSError):
                continue
        return sorted(values, key=lambda item: item.started_at, reverse=True)
