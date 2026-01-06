from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel, Field
from typing import List, Union, Dict, Any
from agent import workflow_app as graph

app = FastAPI(
    title="Travel DeFi Agent",
    version="1.0",
    description="A LangGraph agent hosted on Render"
)

# 1. CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def redirect_to_docs():
    return RedirectResponse(url="/docs")

# --- 2. UPDATED INPUT ADAPTER ---

class SimpleInput(BaseModel):
    messages: List[Dict[str, Any]] = Field(
        ..., 
        description="Chat history. Example: [{'role': 'user', 'content': 'Book a hotel in Paris'}]"
    )

def input_adapter(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Filters out empty messages and ensures the agent receives valid human input."""
    # 1. Extract messages from the incoming request
    raw_messages = input_data.get("messages", [])
    
    # 2. Convert and FILTER blank messages
    converted = []
    for m in raw_messages:
        content = ""
        role = "user"
        
        if isinstance(m, dict):
            # Check for standard role keys or LangChain message types
            role = m.get("role", m.get("type", "user"))
            content = m.get("content", "")
        else:
            # Handle direct object attributes
            content = getattr(m, "content", "")
            role = "user"

        # ONLY add messages that actually contain text
        if content.strip():
            if role in ["user", "human"]:
                converted.append(HumanMessage(content=content))
            elif role in ["assistant", "ai"]:
                converted.append(AIMessage(content=content))

    # 3. Fallback: If list is empty, check for an 'input' field (common in some UI versions)
    if not converted:
        fallback_query = input_data.get("input", "")
        if fallback_query:
            converted.append(HumanMessage(content=fallback_query))

    # 4. Final safety check: if still empty, provide a blank HumanMessage to keep Graph happy
    if not converted:
        converted.append(HumanMessage(content=""))

    # 5. Return the full state expected by AgentState in workflow/graph.py
    return {
        "messages": converted,
        "user_query": converted[-1].content if converted else "",
        "destination": "unknown", 
        "budget_usd": 0.0,
        "hotel_name": "none",
        "hotel_price": 0.0,
        "needs_swap": False,
        "swap_amount": 0.0,
        "final_status": "started",
        "tx_hash": "none"
    }

# 3. Create the Runnable chain with the adapter and the compiled graph
clean_agent = RunnableLambda(input_adapter).with_types(input_type=SimpleInput) | graph

# 4. Add Routes
add_routes(
    app,
    clean_agent,
    path="/agent",
)

if __name__ == "__main__":
    import uvicorn
    # Use standard 8000 for local, Render uses $PORT
    uvicorn.run(app, host="0.0.0.0", port=8000)