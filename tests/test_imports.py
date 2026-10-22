"""Smoke tests for package and dependency imports; no API calls."""

from importlib import import_module

import pytest


@pytest.mark.parametrize("module_name", [
    "workflow_studio", "workflow_studio.config", "workflow_studio.__main__",
    "workflow_studio.agents", "workflow_studio.workflows", "workflow_studio.llm",
    "workflow_studio.state", "workflow_studio.web", "workflow_studio.exporters",
    "workflow_studio.llm.deepseek_client", "workflow_studio.llm.errors", "workflow_studio.llm.smoke_test",
    "workflow_studio.agents.requirements_agent", "workflow_studio.agents.prompts.requirements",
    "workflow_studio.state.requirements",
    "workflow_studio.agents.design_agent", "workflow_studio.agents.prompts.design",
    "workflow_studio.state.design",
    "workflow_studio.agents.coding_agent", "workflow_studio.agents.prompts.coding",
    "workflow_studio.state.coding", "workflow_studio.state.validation", "workflow_studio.exporters.paths",
    "workflow_studio.state.workflow",
    "workflow_studio.state.result", "workflow_studio.workflows.graph",
    "workflow_studio.workflows.nodes", "workflow_studio.workflows.runner",
    "workflow_studio.workflows.integration_test",
    "langchain", "langgraph.graph", "langchain_deepseek", "streamlit", "pydantic", "dotenv",
])
def test_module_imports(module_name: str) -> None:
    assert import_module(module_name) is not None


def test_required_public_interfaces_import() -> None:
    from langchain_deepseek import ChatDeepSeek
    from langgraph.graph import StateGraph

    assert callable(ChatDeepSeek)
    assert callable(StateGraph)
