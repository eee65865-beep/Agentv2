"""Shared LLM access for future Agents."""

from workflow_studio.llm.deepseek_client import DeepSeekClient, get_llm

__all__ = ["DeepSeekClient", "get_llm"]
