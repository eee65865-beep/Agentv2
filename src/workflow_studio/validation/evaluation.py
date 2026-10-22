"""Fixed denominator evaluation of pre-generated candidates, no model calls."""

from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid5, NAMESPACE_URL

from pydantic import BaseModel, ConfigDict, Field

from workflow_studio.state.coding import CodingOutput
from workflow_studio.state.design import DesignOutput, ModuleDesign
from workflow_studio.state.requirements import RequirementsOutput
from workflow_studio.state.result import WorkflowResult
from workflow_studio.storage.paths import guard_no_links
from workflow_studio.validation.models import ValidationLimits, ValidationReport
from workflow_studio.validation.service import validate_artifact
from workflow_studio.validation.suites import SUITES, SUITE_VERSION


class EvaluationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    suite_id: str
    status: str
    report: ValidationReport | None = None
    reason: str


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_version: str = SUITE_VERSION
    dataset_digest: str = Field(default_factory=lambda: sha256("\n".join(suite.id + ":" + suite.digest for suite in SUITES).encode()).hexdigest())
    entries: tuple[EvaluationEntry, ...]
    elapsed_seconds: float

    @property
    def metrics(self):
        counts = {name: sum(item.status == name for item in self.entries) for name in ("passed", "failed", "error", "timeout", "not_run")}
        return {"tasks": len(self.entries), "automatable_tasks": sum(not suite.manual for suite in SUITES), **counts,
                "pass_rate_all_tasks": counts["passed"] / len(self.entries) if self.entries else 0.0, "model_calls": 0}


def candidate_result(code, suite):
    """Synthetic fixture context is explicitly distinct from LLM task outputs."""
    req = RequirementsOutput(project_name=suite.title, summary=suite.contract, functional_requirements=[suite.contract], non_functional_requirements=[], constraints=["Python 标准库"], assumptions=[], acceptance_criteria=[suite.contract])
    design = DesignOutput(architecture_overview="固定 CLI 评测适配器", technology_stack=["Python"], modules=[ModuleDesign(name="main", responsibility=suite.contract, dependencies=[], requirement_ids=["FR-1"])], data_models=[], interfaces=["JSON stdin/stdout"], project_structure="main.py", design_decisions=["固定评测"], preserved_constraints=req.constraints)
    return WorkflowResult(run_id=str(uuid5(NAMESPACE_URL, SUITE_VERSION + "/" + suite.id)), status="completed", requirements=req, design=design, code=code, events=[], error=None)


def evaluate_candidates(directory: Path, *, backend=None, limits=None, cancel=None):
    """Read <suite-id>.json CodingOutput files. Missing tasks remain in denominator.

    The suite count and cases are selected from the application registry, never
    from candidate code. Total cap is 300 seconds for the whole evaluation.
    """
    began = monotonic()
    root = Path(directory).absolute()
    guard_no_links(root)
    entries = []
    for suite in SUITES:
        if suite.manual:
            entries.append(EvaluationEntry(suite_id=suite.id, status="not_run", reason="manual_required"))
            continue
        path = root / (suite.id + ".json")
        if monotonic() - began >= 300 or (cancel is not None and cancel.is_set()):
            entries.append(EvaluationEntry(suite_id=suite.id, status="not_run", reason="evaluation_budget"))
            continue
        if not path.exists():
            entries.append(EvaluationEntry(suite_id=suite.id, status="not_run", reason="missing_candidate"))
            continue
        try:
            guard_no_links(path)
            if path.stat().st_size > 1048576:
                raise ValueError("candidate_too_large")
            code = CodingOutput.model_validate_json(path.read_bytes())
            chosen = limits if limits is not None else ValidationLimits()
            chosen = chosen.model_copy(update={"total_seconds": min(chosen.total_seconds, max(1.0, 300 - (monotonic() - began)))})
            report = validate_artifact(candidate_result(code, suite), suite.id, backend=backend, limits=chosen, cancel=cancel)
            entries.append(EvaluationEntry(suite_id=suite.id, status=report.status, report=report, reason=report.reason))
        except (ValueError, OSError):
            entries.append(EvaluationEntry(suite_id=suite.id, status="error", reason="invalid_candidate"))
    return EvaluationResult(entries=tuple(entries), elapsed_seconds=monotonic() - began)
