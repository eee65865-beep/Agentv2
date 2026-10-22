"""Standard three-node graph and optional bounded conditional review graph."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from workflow_studio.state.workflow import WorkflowState
from workflow_studio.workflows.nodes import requirements_agent_node, design_agent_node, coding_agent_node, review_agent_node
from workflow_studio.workflows.routing import route_after_coding, route_after_review


def build_workflow(*, review_enabled: bool = False) -> CompiledStateGraph:
    """Expose invoke/ainvoke and stream/astream from LangGraph itself."""
    if type(review_enabled) is not bool:
        raise ValueError("review_enabled must be a boolean")
    graph = StateGraph(WorkflowState)
    graph.add_node("requirements_agent", requirements_agent_node)
    graph.add_node("design_agent", design_agent_node)
    graph.add_node("coding_agent", coding_agent_node)
    graph.add_edge(START, "requirements_agent")
    graph.add_edge("requirements_agent", "design_agent")
    graph.add_edge("design_agent", "coding_agent")
    if review_enabled:
        graph.add_node("review_agent", review_agent_node)
        graph.add_conditional_edges("coding_agent", route_after_coding, {"review_agent": "review_agent", "__end__": END})
        graph.add_conditional_edges("review_agent", route_after_review, {"coding_agent": "coding_agent", "__end__": END})
    else:
        graph.add_edge("coding_agent", END)
    return graph.compile()
