"""Post-generation acceptance service, separate from terminal run archives."""

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from workflow_studio.state.result import WorkflowResult
from workflow_studio.state.review_policy import artifact_digest, review_context_digest, evaluate_review
from workflow_studio.validation.docker import BackendError, DockerBackend
from workflow_studio.validation.models import CaseResult, ValidationLimits, ValidationReport
from workflow_studio.validation.suites import get_suite


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("invalid_constant")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def _equal(left, right):
    # JSON bool and numeric values are different; numeric 2 and 2.0 may agree.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _stdlib_only(code):
    if any(item.casefold().strip() not in {"stdlib", "python", "python standard library", "标准库", "python 标准库"} for item in code.dependencies):
        return False
    modules = {item.path.split("/")[0].removesuffix(".py") for item in code.files}
    allowed = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    for file in code.files:
        if not file.path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(file.content)):
            names = [item.name for item in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            if any(name.split(".")[0] not in allowed | modules for name in names):
                return False
    return True


def validate_artifact(result: WorkflowResult, suite_id: str, *, backend=None, limits=None, cancel=None):
    """Explicit opt-in only. No backend => not_run, never a host fallback.

    This is a separate post-generation operation with its own bounded budget;
    it cannot change the original terminal run, repair counters or archives.
    """
    result = WorkflowResult.model_validate(result.model_dump())
    if result.code is None or result.requirements is None or result.design is None:
        raise ValueError("validation_requires_complete_artifacts")
    suite = get_suite(suite_id)
    limits = ValidationLimits() if limits is None else ValidationLimits.model_validate(limits.model_dump())
    began, started = monotonic(), datetime.now(timezone.utc)
    environment = "unavailable"
    prepared = False
    cases = []
    def report(status, reason):
        return ValidationReport(run_id=result.run_id, artifact_digest=artifact_digest(result.code),
            context_digest=review_context_digest(result.requirements, result.design, result.code), suite_id=suite.id,
            suite_digest=suite.digest, expected_cases=len(suite.cases), status=status, reason=reason,
            environment=environment, cases=tuple(cases), limits=limits, started_at=started, elapsed_seconds=monotonic() - began)
    if suite.manual:
        return report("not_run", "manual_required")
    if not suite.cases:
        return report("not_run", "no_cases")
    if len(suite.cases) > limits.max_cases:
        return report("not_run", "case_limit")
    if not set(suite.required_files).issubset({item.path for item in result.code.files}):
        return report("not_run", "unsupported_layout")
    if not _stdlib_only(result.code):
        return report("not_run", "unsupported_dependencies")
    try:
        backend = backend if backend is not None else DockerBackend()
        with TemporaryDirectory(prefix="workflow-validation-") as directory:
            root = Path(directory)
            submission, config = root / "submission", root / "docker-config"
            submission.mkdir(mode=0o755)
            config.mkdir()
            # Copy only revalidated generated text, never the host checkout or keys.
            for item in result.code.files:
                path = submission.joinpath(*item.path.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                path.write_bytes(item.content.encode("utf-8"))
                path.chmod(0o644)
            backend.prepare(config, limits.total_seconds - (monotonic() - began), cancel)
            prepared = True
            environment = backend.environment
            for case in suite.cases:
                remaining = limits.total_seconds - (monotonic() - began)
                if remaining <= 0:
                    return report("timeout", "deadline")
                if cancel is not None and cancel.is_set():
                    return report("error", "cancelled")
                clock = monotonic()
                captured, storage_ok = backend.run_case(suite, case, submission, config, limits, remaining, cancel)
                reason = captured.reason
                if reason is not None:
                    status = "timeout" if reason == "deadline" else ("failed" if reason in ("output_limit", "resource_limit") else "error")
                elif captured.exit_code is None:
                    status, reason = "error", "runtime_error"
                elif captured.exit_code != 0:
                    status, reason = "failed", "program_exit"
                elif not storage_ok:
                    status, reason = "failed", "storage_mismatch"
                else:
                    try:
                        matches = _equal(_json(captured.stdout), _json(case.expected))
                        status, reason = ("passed", "matched") if matches else ("failed", "assertion_failed")
                    except (ValueError, UnicodeError, RecursionError):
                        status, reason = "failed", "invalid_json"
                cases.append(CaseResult(case_id=case.id, status=status, reason=reason, exit_code=captured.exit_code, elapsed_seconds=monotonic() - clock))
                if status in ("error", "timeout"):
                    return report(status, "deadline" if status == "timeout" else ("cancelled" if reason == "cancelled" else "runtime_error"))
        return report("passed" if all(item.status == "passed" for item in cases) else "failed", "finished")
    except BackendError as error:
        reason = str(error)
        if reason in ("image_unconfigured", "backend_unavailable") and not prepared:
            return report("not_run", reason)
        return report("timeout" if reason == "deadline" else "error", reason if reason in ("deadline", "cancelled") else "runtime_error")
    except OSError:
        return report("error", "runtime_error")


def validate_report_binding(report, requirements, design, code):
    report = ValidationReport.model_validate(report.model_dump())
    suite = get_suite(report.suite_id)
    if report.artifact_digest != artifact_digest(code) or report.context_digest != review_context_digest(requirements, design, code) or report.suite_digest != suite.digest or report.expected_cases != len(suite.cases):
        raise ValueError("stale_validation_evidence")
    expected = tuple(item.id for item in suite.cases)
    actual = tuple(item.case_id for item in report.cases)
    if actual != expected[:len(actual)] or (report.reason == "finished" and actual != expected):
        raise ValueError("incomplete_validation_evidence")


def validation_quality(result, report):
    """Execution status is independent of the original run status/static review."""
    if report is None:
        return "not_run"
    try:
        if result.run_id != report.run_id:
            return "stale"
        validate_report_binding(report, result.requirements, result.design, result.code)
    except (ValueError, AttributeError):
        return "stale"
    if report.status != "passed":
        return report.status
    if result.status != "completed" or (result.review is not None and not evaluate_review(result.review).passed):
        return "failed"
    return "passed"
