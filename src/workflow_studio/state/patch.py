"""Whole-file incremental patches; pure application, no filesystem execution."""

from hashlib import sha256
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from workflow_studio.exporters.paths import validate_export_path
from workflow_studio.state.coding import CodeFile, CodingOutput, MAX_FILES, MAX_OUTPUT_BYTES, utf8_size
from workflow_studio.state.review_policy import artifact_digest

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
IssueId = Annotated[str, StringConstraints(pattern=r"^(RV-[1-9][0-9]*|LEGACY-[1-9][0-9]*|VT-[a-z][a-z0-9-]{0,63})$")]
Operation = Literal["replace", "create", "delete"]


class PatchError(ValueError):
    """Allowlisted codes only; no source, raw response or private values."""


def digest_json(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    operation: Operation = Field(description="文件操作；默认策略仅允许 replace，新增/删除须调用方明确授权。")
    path: str = Field(max_length=240, description="精确、安全、大小写一致的相对路径。")
    file: CodeFile | None = Field(description="replace/create 必须提供完整文件，路径等于 path；delete 必须为 null。")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    issue_ids: list[IssueId] = Field(min_length=1, max_length=64, description="仅引用当前可信修复上下文提供的问题编号。")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return validate_export_path(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.operation == "delete":
            if self.file is not None:
                raise ValueError("deletion_cannot_supply_content")
        elif self.file is None or self.file.path != self.path:
            raise ValueError("change_requires_matching_complete_file")
        if len(set(self.issue_ids)) != len(self.issue_ids):
            raise ValueError("duplicate_patch_issue_reference")
        return self


class CodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    contract_version: Literal[1]
    base_artifact_digest: Digest
    base_context_digest: Digest
    feedback_digest: Digest
    changes: list[FileChange] = Field(min_length=1, max_length=MAX_FILES)

    @field_validator("contract_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("patch_version_must_be_integer")
        return value

    @model_validator(mode="after")
    def bounded_unique_changes(self) -> Self:
        if len({item.path.casefold() for item in self.changes}) != len(self.changes):
            raise ValueError("duplicate_patch_target")
        if utf8_size(self.model_dump_json()) > MAX_OUTPUT_BYTES:
            raise ValueError("patch_output_too_large")
        return self


class RepairPolicy(BaseModel):
    """Trusted caller policy, not a field the model can widen."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    replace_paths: tuple[str, ...]
    create_paths: tuple[str, ...] = ()
    delete_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_paths(self) -> Self:
        groups = (self.replace_paths, self.create_paths, self.delete_paths)
        for values in groups:
            if len(values) > MAX_FILES or len(set(item.casefold() for item in values)) != len(values):
                raise ValueError("invalid_patch_policy_paths")
            for path in values:
                validate_export_path(path)
        if set(self.replace_paths) & set(self.delete_paths):
            raise ValueError("contradictory_patch_policy")
        return self

    @classmethod
    def replacements_only(cls, code):
        return cls(replace_paths=tuple(file.path for file in code.files))


class ChangeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Operation
    path: str
    issue_ids: tuple[IssueId, ...] = Field(min_length=1, max_length=64)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return validate_export_path(value)


class RepairReceipt(BaseModel):
    """No code/raw prompts in event telemetry; bytes are not measured tokens."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    base_artifact_digest: Digest
    result_artifact_digest: Digest
    context_digest: Digest
    feedback_digest: Digest
    patch_digest: Digest
    changes: tuple[ChangeSummary, ...] = Field(min_length=1, max_length=MAX_FILES)
    preserved_file_count: int = Field(ge=0, le=MAX_FILES, strict=True)
    input_context_bytes: int = Field(ge=1, strict=True)
    normalized_patch_bytes: int = Field(ge=1, le=MAX_OUTPUT_BYTES, strict=True)
    merged_artifact_bytes: int = Field(ge=1, le=MAX_OUTPUT_BYTES, strict=True)
    format_failures: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def unique_changes(self) -> Self:
        if len({item.path.casefold() for item in self.changes}) != len(self.changes):
            raise ValueError("duplicate_receipt_change")
        return self


def apply_code_patch(base, patch, *, context_digest, feedback_digest, allowed_issue_ids, policy=None):
    """Validate everything before publishing a new immutable full artifact."""
    base = CodingOutput.model_validate(base.model_dump())
    patch = CodePatch.model_validate(patch.model_dump())
    policy = RepairPolicy.replacements_only(base) if policy is None else RepairPolicy.model_validate(policy.model_dump())
    if patch.base_artifact_digest != artifact_digest(base):
        raise PatchError("patch_base_artifact_mismatch")
    if patch.base_context_digest != context_digest or patch.feedback_digest != feedback_digest:
        raise PatchError("patch_context_or_feedback_mismatch")
    existing = {file.path: file for file in base.files}
    allowed = {"replace": set(policy.replace_paths), "create": set(policy.create_paths), "delete": set(policy.delete_paths)}
    # Validate policy against the base before looking at model operations.
    if (allowed["replace"] | allowed["delete"]) - existing.keys() or any(path.casefold() in {name.casefold() for name in existing} for path in allowed["create"]):
        raise PatchError("patch_policy_does_not_match_base")
    changed = dict(existing)
    for change in patch.changes:
        if change.path not in allowed[change.operation]:
            raise PatchError("patch_operation_outside_policy")
        if set(change.issue_ids) - set(allowed_issue_ids):
            raise PatchError("patch_unknown_issue_reference")
        if change.operation == "delete":
            del changed[change.path]
        else:
            if change.operation == "replace" and change.file.content == existing[change.path].content:
                raise PatchError("patch_replacement_has_no_change")
            changed[change.path] = change.file
    # Original order and all metadata are preserved; approved new files append.
    merged = CodingOutput.model_validate({**base.model_dump(), "files": [file.model_dump() for file in changed.values()]})
    if artifact_digest(merged) == artifact_digest(base):
        raise PatchError("patch_has_no_effect")
    return merged


def validate_repair_receipts(events, current_code, history, revision_count, requirements, design):
    """Check event receipts against actual saved versions, never infer success."""
    from workflow_studio.state.requirements import RequirementsOutput
    from workflow_studio.state.design import DesignOutput
    from workflow_studio.state.review_policy import review_context_digest
    receipts = [event for event in events if event.repair_receipt is not None]
    if not receipts:
        return
    records = {record.revision_count: record for record in history}
    versions = {number: record.code for number, record in records.items()}
    if current_code is not None:
        versions[revision_count] = CodingOutput.model_validate(current_code)
    seen = set()
    for event in receipts:
        revision = event.revision_count
        receipt = event.repair_receipt
        if revision in seen or revision not in versions or revision - 1 not in records:
            raise ValueError("patch_receipt_has_no_unique_version_boundary")
        seen.add(revision)
        base, result = records[revision - 1].code, versions[revision]
        if receipt.base_artifact_digest != artifact_digest(base) or receipt.result_artifact_digest != artifact_digest(result):
            raise ValueError("patch_receipt_artifact_mismatch")
        if receipt.context_digest != review_context_digest(RequirementsOutput.model_validate(requirements), DesignOutput.model_validate(design), base):
            raise ValueError("patch_receipt_context_mismatch")
        expected_feedback = digest_json({"review": records[revision - 1].review.model_dump(mode="json"), "validation_report": None})
        if receipt.feedback_digest != expected_feedback:
            raise ValueError("patch_receipt_feedback_mismatch")
        before, after = {file.path: file for file in base.files}, {file.path: file for file in result.files}
        differences = {(name, "delete") for name in before.keys() - after.keys()} | {(name, "create") for name in after.keys() - before.keys()}
        differences |= {(name, "replace") for name in before.keys() & after.keys() if before[name] != after[name]}
        if differences != {(item.path, item.operation) for item in receipt.changes}:
            raise ValueError("patch_receipt_changes_mismatch")
        expected_preserved = len(base.files) - sum(item.operation in ("replace", "delete") for item in receipt.changes)
        if receipt.preserved_file_count != expected_preserved or receipt.merged_artifact_bytes != utf8_size(result.model_dump_json()):
            raise ValueError("patch_receipt_measurement_mismatch")
        if any(getattr(base, field) != getattr(result, field) for field in ("instructions", "dependencies", "test_suggestions", "limitations")):
            raise ValueError("patch_receipt_cannot_change_artifact_metadata")
