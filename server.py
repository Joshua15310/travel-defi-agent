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

# --- SIMPLIFIED INPUT ADAPTER ---

class SimpleInput(BaseModel):
    # Reverting to just messages so the UI is clean
    messages: List[Dict[str, Any]] = Field(
        default=[], 
        description="Chat history. Click '+' to add a message."
    )

def input_adapter(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures the agent gets the user query from the message list."""
    raw_messages = input_data.get("messages", [])
    converted = []
    
    for m in raw_messages:
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if content.strip():
            converted.append(HumanMessage(content=content))

    # Fallback to keep the graph from crashing if empty
    if not converted:
        converted.append(HumanMessage(content=""))

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

# 3. Create the Runnable chain
clean_agent = RunnableLambda(input_adapter).with_types(input_type=SimpleInput) | graph

# 4. Add Routes - SET TO DEFAULT PLAYGROUND
add_routes(
    app,
    clean_agent,
    path="/agent",
    playground_type="default" # <--- This fixes the "Chat playground not supported" error
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)