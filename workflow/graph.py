from langgraph.graph import StateGraph, END
from typing import TypedDict, List, Optional
from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    messages: List[BaseMessage]
    user_query: str
    destination: str
    budget_usd: float
    hotels: List[dict]
    hotel_name: Optional[str]
    hotel_price: Optional[float]
    needs_swap: bool
    swap_amount: float
    final_status: str
    tx_hash: str

def build_workflow(parse_intent_node, search_hotels_node, check_swap_node, book_hotel_node, checkpointer=None):
    # 1. Initialize Graph
    workflow = StateGraph(AgentState)

    # 2. Add Nodes
    workflow.add_node("parse", parse_intent_node)
    workflow.add_node("search", search_hotels_node)
    workflow.add_node("swap", check_swap_node)
    workflow.add_node("book", book_hotel_node)

    # 3. Define Edges
    workflow.set_entry_point("parse")
    
    workflow.add_edge("parse", "search")
    
    # Conditional logic after search
    def route_after_search(state):
        # If the user selected a hotel (hotel_name is set), go to check swap/book
        if state.get("hotel_name"):
            return "swap"
        # Otherwise (just a list of hotels), stop and wait for user selection
        return END

    workflow.add_conditional_edges(
        "search",
        route_after_search,
        {
            "swap": "swap",
            END: END
        }
    )

    workflow.add_edge("swap", "book")
    workflow.add_edge("book", END)

    # 4. Compile with Memory (Checkpointer) - FIXED LINE BELOW
    app = workflow.compile(checkpointer=checkpointer)
    
    return app