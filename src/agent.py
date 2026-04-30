import os
from langgraph.graph import StateGraph, START, END
from src.shared.shared_constants import logger, ENRICH_DATA
from src.classes.agent_state import AgentState
from src.nodes.node_context_manager import node_context_manager
from src.nodes.node_solution_mapper import node_solution_mapper
from src.nodes.node_commit_filter import node_commit_filter
from src.nodes.node_roslyn_preprocessor import node_roslyn_preprocessor
from src.nodes.node_llm_chunker import node_llm_chunker
from src.nodes.node_json_exporter import node_json_exporter

# ---- CONDITIONAL ROUTING ----
def _route_after_preprocessing(state) -> str:
    """
    After Roslyn/XML preprocessing, route based on the enrich_data config flag:
    - If True  → run LLM enrichment (llm_chunker)
    - If False → skip directly to JSON export
    """
    if ENRICH_DATA:
        return "llm_chunker"
    return "json_exporter"

# ---- BUILD LANGGRAPH PIPELINE ----
workflow = StateGraph(AgentState)

workflow.add_node("context_manager",    node_context_manager)
workflow.add_node("solution_mapper",    node_solution_mapper)
workflow.add_node("commit_filter",      node_commit_filter)
workflow.add_node("roslyn_preprocessor", node_roslyn_preprocessor)
workflow.add_node("llm_chunker",        node_llm_chunker)
workflow.add_node("json_exporter",      node_json_exporter)

# Define edges
workflow.add_edge(START,               "context_manager")
workflow.add_edge("context_manager",   "solution_mapper")
workflow.add_edge("solution_mapper",   "commit_filter")
workflow.add_edge("commit_filter",     "roslyn_preprocessor")

# Conditional branch: enrich_data toggle
workflow.add_conditional_edges(
    "roslyn_preprocessor",
    _route_after_preprocessing,
    {
        "llm_chunker":   "llm_chunker",
        "json_exporter": "json_exporter",
    }
)

workflow.add_edge("llm_chunker",  "json_exporter")
workflow.add_edge("json_exporter", END)

app = workflow.compile()

if __name__ == "__main__":
    logger.info(f"Starting DroidAgent Pipeline... (enrich_data={ENRICH_DATA})")
    result = app.invoke({
        "valid_project_dirs": [],
        "commits": []
    })
    logger.info("Pipeline Finished Successfully!")
