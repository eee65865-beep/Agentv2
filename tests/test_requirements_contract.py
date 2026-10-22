"""Stable traceability, grounded generation and on-disk legacy compatibility."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from workflow_studio.agents.requirements_agent import analyze_requirements, RequirementsOutputError
from workflow_studio.agents.design_agent import design_system
from workflow_studio.config import Settings
from workflow_studio.errors import CorruptRunError
from workflow_studio.exporters.documents import requirements_markdown
from workflow_studio.llm.errors import LLMStructuredOutputUnsupported
from workflow_studio.state import CodeFile, CodingOutput, DesignOutput, ModuleDesign, RequirementsOutput, RequirementsOutputV2
from workflow_studio.state.requirements import RequirementsSourceError, validate_requirement_sources
from workflow_studio.state.validation import DesignConsistencyError, validate_design_requirements
from workflow_studio.state.workflow import create_initial_state, validate_update, WorkflowStateError
from workflow_studio.storage import RunStore
from workflow_studio.workflows import run_workflow
from workflow_studio.web.app import SESSION_KEY

REQUEST = "使用 Python 实现新增任务和删除任务，仅在本地保存。"


@pytest.fixture
def v2_data():
    return {
        "project_name": "任务工具", "summary": "本地管理任务",
        "functional_requirements": ["新增任务", "删除任务"],
        "non_functional_requirements": [], "constraints": ["仅在本地保存"], "assumptions": [],
        "acceptance_criteria": ["新增后可见", "删除后不可见"], "contract_version": 2, "open_questions": [],
        "requirement_details": [
            {"id": "FR-7", "description": "新增任务", "source_quotes": ["新增任务"]},
            {"id": "FR-20", "description": "删除任务", "source_quotes": ["删除任务"]},
        ],
        "acceptance_details": [
            {"id": "AC-3", "description": "新增后可见", "requirement_ids": ["FR-7"]},
            {"id": "AC-9", "description": "删除后不可见", "requirement_ids": ["FR-20"]},
        ],
    }


def legacy_data(data):
    return {key: value for key, value in data.items() if key not in {"contract_version", "requirement_details", "acceptance_details"}}


def design_for(requirements):
    return DesignOutput(
        architecture_overview="本地单模块", technology_stack=["Python"],
        modules=[ModuleDesign(name="tasks", responsibility="管理任务", dependencies=[], requirement_ids=[item["id"] for item in requirements.requirement_catalog()])],
        data_models=[], interfaces=[], project_structure="tasks.py", design_decisions=["保持简单"],
        preserved_constraints=requirements.constraints,
    )


def test_persisted_ids_survive_independent_reordering_and_roundtrip(v2_data):
    before = RequirementsOutput.model_validate(v2_data)
    v2_data["functional_requirements"].reverse()
    v2_data["acceptance_criteria"].reverse()
    v2_data["requirement_details"].reverse()
    v2_data["acceptance_details"].reverse()
    after = RequirementsOutput.model_validate_json(json.dumps(v2_data))
    assert {item["id"]: item["description"] for item in before.requirement_catalog()} == {item["id"]: item["description"] for item in after.requirement_catalog()}
    assert {item.id: item.requirement_ids for item in before.acceptance_details} == {item.id: item.requirement_ids for item in after.acceptance_details}
    assert validate_design_requirements(design_for(before), after)


@pytest.mark.parametrize("problem", [
    "duplicate_fr", "duplicate_ac", "unknown_ref", "duplicate_ref", "missing_coverage",
    "missing_details", "fr_text", "ac_text", "legacy_with_details", "version", "extra", "empty_source",
])
def test_rejects_inconsistent_traceability(v2_data, problem):
    if problem == "duplicate_fr":
        v2_data["requirement_details"][1]["id"] = "FR-7"
    elif problem == "duplicate_ac":
        v2_data["acceptance_details"][1]["id"] = "AC-3"
    elif problem == "unknown_ref":
        v2_data["acceptance_details"][1]["requirement_ids"] = ["FR-99"]
    elif problem == "duplicate_ref":
        v2_data["acceptance_details"][0]["requirement_ids"] = ["FR-7", "FR-7"]
    elif problem == "missing_coverage":
        v2_data["acceptance_details"][1]["requirement_ids"] = ["FR-7"]
    elif problem == "missing_details":
        v2_data.pop("requirement_details")
    elif problem == "fr_text":
        v2_data["requirement_details"][1]["description"] = "未确认功能"
    elif problem == "ac_text":
        v2_data["acceptance_details"][1]["description"] = "另一项验收"
    elif problem == "legacy_with_details":
        v2_data["contract_version"] = 1
    elif problem == "version":
        v2_data["contract_version"] = 3
    elif problem == "extra":
        v2_data["requirement_details"][0]["untrusted"] = True
    elif problem == "empty_source":
        v2_data["requirement_details"][0]["source_quotes"] = []
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate(v2_data)


@pytest.mark.parametrize("value", [True, False, 1.0, 2.0, "2", None])
def test_version_is_strict_integer(v2_data, value):
    v2_data["contract_version"] = value
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate(v2_data)


def test_legacy_read_does_not_invent_provenance_or_coverage(v2_data):
    old = RequirementsOutput.model_validate(legacy_data(v2_data))
    assert old.contract_version == 1
    assert old.requirement_details == old.acceptance_details == []
    assert [item["id"] for item in old.requirement_catalog()] == ["FR-1", "FR-2"]
    assert "不推测历史映射" in requirements_markdown(old)
    with pytest.raises(ValidationError):
        RequirementsOutputV2.model_validate(legacy_data(v2_data))


def test_generation_schema_requires_traceability():
    schema = RequirementsOutputV2.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_unknown_source_rejected_at_agent_and_state_boundaries(v2_data, monkeypatch):
    v2_data["requirement_details"][0]["source_quotes"] = ["private-invented-source"]
    output = RequirementsOutputV2.model_validate(v2_data)
    with pytest.raises(RequirementsSourceError) as error:
        validate_requirement_sources(output, REQUEST)
    assert "private" not in str(error.value)
    with pytest.raises(WorkflowStateError) as error:
        validate_update(create_initial_state(REQUEST), {"requirements": output.model_dump()})
    assert "private" not in str(error.value)
    client = MagicMock()
    client.invoke_structured.return_value = output
    client.invoke.return_value = AIMessage(content=output.model_dump_json())
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: client)
    with pytest.raises(RequirementsOutputError):
        analyze_requirements(REQUEST, max_repair_attempts=0)
    assert client.invoke.call_count == 1
    feedback = client.invoke.call_args.args[0].to_messages()[2].content
    assert "requirement_source_quote_not_in_request" in feedback


@pytest.mark.parametrize("native", [True, False])
def test_invalid_source_can_be_repaired(v2_data, monkeypatch, native):
    invalid = deepcopy(v2_data)
    invalid["requirement_details"][0]["source_quotes"] = ["虚构来源"]
    client = MagicMock()
    if native:
        client.invoke_structured.return_value = RequirementsOutputV2.model_validate(invalid)
        client.invoke.return_value = AIMessage(content=json.dumps(v2_data))
    else:
        client.invoke_structured.side_effect = LLMStructuredOutputUnsupported("unsupported")
        client.invoke.side_effect = [AIMessage(content=json.dumps(invalid)), AIMessage(content=json.dumps(v2_data))]
    monkeypatch.setattr("workflow_studio.agents.requirements_agent.get_llm", lambda: client)
    assert analyze_requirements(REQUEST).contract_version == 2
    assert client.invoke.call_count == (1 if native else 2)


def test_design_receives_persisted_catalog_and_rejects_positional_ids(v2_data, monkeypatch):
    requirements = RequirementsOutput.model_validate(v2_data)
    client = MagicMock()
    design = design_for(requirements)
    client.invoke_structured.return_value = design
    monkeypatch.setattr("workflow_studio.agents.design_agent.get_llm", lambda: client)
    assert design_system(requirements) == design
    human = client.invoke_structured.call_args.args[0].to_messages()[1].content
    catalog = human.split("功能需求编号表（编号用于模块追溯，不改变原始需求）：\n")[1].split("\n可选原始需求上下文")[0]
    assert json.loads(catalog) == requirements.requirement_catalog()
    design.modules[0].requirement_ids = ["FR-1", "FR-2"]
    with pytest.raises(DesignConsistencyError, match="unknown_functional_requirement_id"):
        validate_design_requirements(design, requirements)


@pytest.fixture
def make_result(monkeypatch):
    def make(requirements):
        monkeypatch.setattr("workflow_studio.workflows.nodes.analyze_requirements", lambda _: requirements)
        monkeypatch.setattr("workflow_studio.workflows.nodes.design_system", lambda _: design_for(requirements))
        code = CodingOutput(files=[CodeFile(path="tasks.py", language="python", content="tasks = []\n", description="示例产物")], instructions="未执行", dependencies=[], test_suggestions=[], limitations=["未执行"])
        monkeypatch.setattr("workflow_studio.workflows.nodes.generate_code", lambda *_: code)
        return run_workflow(REQUEST)
    return make


def test_v2_workflow_storage_and_export_preserve_ids(v2_data, make_result, tmp_path):
    result = make_result(RequirementsOutput.model_validate(v2_data))
    assert result.status == "completed"
    store = RunStore(tmp_path / "history")
    store.save(result, REQUEST, "mock", 0)
    loaded = store.load(result.run_id).result
    assert loaded == result
    assert loaded.requirements.contract_version == 2
    markdown = requirements_markdown(loaded.requirements)
    assert "FR-7：新增任务" in markdown
    assert "AC-9 → FR-20" in markdown
    assert "需求原文来源" in markdown


def test_original_v1_disk_bytes_load_without_rewriting(v2_data, make_result, tmp_path):
    result = make_result(RequirementsOutput.model_validate(legacy_data(v2_data)))
    store = RunStore(tmp_path / "history")
    directory = store.save(result, REQUEST, "mock", 0)
    requirements_path = directory / "requirements.json"
    original = (json.dumps(legacy_data(v2_data), ensure_ascii=False) + "\n").encode("utf-8")
    requirements_path.write_bytes(original)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"]["requirements.json"] = sha256(original).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    metadata_before = metadata_path.read_bytes()
    loaded = store.load(result.run_id).result
    assert loaded.requirements.contract_version == 1
    assert loaded.requirements.acceptance_details == []
    assert loaded == result
    assert store.save(loaded, REQUEST, "mock", 0) == directory
    assert requirements_path.read_bytes() == original
    assert metadata_path.read_bytes() == metadata_before


def test_valid_digest_cannot_hide_fabricated_provenance(v2_data, make_result, tmp_path):
    result = make_result(RequirementsOutput.model_validate(v2_data))
    store = RunStore(tmp_path / "history")
    directory = store.save(result, REQUEST, "mock", 0)
    forged = deepcopy(v2_data)
    forged["requirement_details"][0]["source_quotes"] = ["并未提出的功能"]
    body = json.dumps(forged, ensure_ascii=False).encode("utf-8")
    (directory / "requirements.json").write_bytes(body)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"]["requirements.json"] = sha256(body).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CorruptRunError):
        store.load(result.run_id)


def test_web_renders_v2_ids_acceptance_and_source_quotes(v2_data, make_result, monkeypatch):
    make_result(RequirementsOutput.model_validate(v2_data))
    monkeypatch.setattr("workflow_studio.web.configuration.load_settings", lambda: Settings(deepseek_api_key="test-only", deepseek_model="mock"))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    at.text_area(key="request_input").input(REQUEST).run()
    at.button(key="start_workflow").click().run()
    page = at.session_state[SESSION_KEY]
    page.task.thread.join(timeout=10)
    assert not page.task.thread.is_alive()
    at.run()
    assert not at.exception
    assert page.result.status == "completed"
    text = "\n".join(item.value for item in at.markdown)
    assert "FR-7：新增任务" in text
    assert "AC-9 → FR-20：删除后不可见" in text
    assert "需求契约 v2" in "\n".join(item.value for item in at.caption)
