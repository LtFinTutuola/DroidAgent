import os
import atexit
from langgraph.graph import StateGraph, START, END
from src.shared.shared_constants import logger
from src.classes.agent_state import AgentState
from src.nodes.node_context_manager import node_context_manager
from src.nodes.node_solution_mapper import node_solution_mapper
from src.nodes.node_commit_filter import node_commit_filter
from src.nodes.node_roslyn_preprocessor import node_roslyn_preprocessor
from src.nodes.node_llm_chunker import node_llm_chunker
from src.nodes.node_json_exporter import node_json_exporter

# ---- BUILD LANGGRAPH PIPELINE ----
workflow = StateGraph(AgentState)

workflow.add_node("context_manager", node_context_manager)
workflow.add_node("solution_mapper", node_solution_mapper)
workflow.add_node("commit_filter", node_commit_filter)
workflow.add_node("roslyn_preprocessor", node_roslyn_preprocessor)
workflow.add_node("llm_chunker", node_llm_chunker)
workflow.add_node("json_exporter", node_json_exporter)

# Define edges
workflow.add_edge(START, "context_manager")
workflow.add_edge("context_manager", "solution_mapper")
workflow.add_edge("solution_mapper", "commit_filter")
workflow.add_edge("commit_filter", "roslyn_preprocessor")
workflow.add_edge("roslyn_preprocessor", "llm_chunker")
workflow.add_edge("llm_chunker", "json_exporter")
workflow.add_edge("json_exporter", END)

app = workflow.compile()

if __name__ == "__main__":
    logger.info("Starting DroidAgent Pipeline...")
    result = app.invoke({
        "valid_project_dirs": [],
        "commits": []
    })
    logger.info("Pipeline Finished Successfully!")
