"""
Explicit LangGraph workflow definition for the Crypto Travel Agent.
This file makes the graph wiring explicit (nodes, edges, entry point)
so reviewers can quickly see the LangGraph integration.

It imports the node functions from `agent.py` and exports a compiled
workflow called `workflow_app` which can be used by external tooling
or the README as a canonical entry point for tracing.
"""
from langgraph.graph import StateGraph, END

# Import node functions and AgentState type from the main agent module.
# Importing `agent` here is intentionally lightweight: this module only
# references the already-defined functions in `agent.py` and registers
# them in the StateGraph.
from agent import AgentState, parse_intent, search_hotels, check_swap, book_hotel


def build_workflow():
    wf = StateGraph(AgentState)
    wf.add_node("parse", parse_intent)
    wf.add_node("search", search_hotels)
    wf.add_node("swap", check_swap)
    wf.add_node("book", book_hotel)

    wf.set_entry_point("parse")
    wf.add_edge("parse", "search")
    wf.add_edge("search", "swap")
    wf.add_edge("swap", "book")
    wf.add_edge("book", END)

    return wf.compile()


# Export a ready-to-use compiled workflow for tooling and reviewers.
workflow_app = build_workflow()
