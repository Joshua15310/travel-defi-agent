from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage, AIMessage
from pydantic import BaseModel, Field
from typing import List, Union, Dict, Any

# Import workflow_app from agent.py (this was the source of your error)
from agent import workflow_app as graph

app = FastAPI(
    title="Crypto Travel DeFi Agent",
    version="1.0",
    description="A LangGraph agent hosted on Render"
)

# CORS Setup
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

# --- INPUT ADAPTER ---
class SimpleInput(BaseModel):
    messages: List[Dict[str, Any]] = Field(
        default=[], 
        description="Chat history. Click '+' to add a message."
    )

def input_adapter(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures the agent receives valid input and filters empty messages."""
    raw_messages = input_data.get("messages", [])
    converted = []
    
    for m in raw_messages:
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if content.strip():
            converted.append(HumanMessage(content=content))

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

# Create the Runnable chain with the adapter and the compiled graph
clean_agent = RunnableLambda(input_adapter).with_types(input_type=SimpleInput) | graph

# Add Routes
add_routes(
    app,
    clean_agent,
    path="/agent",
    playground_type="default"
)

if __name__ == "__main__":
    import uvicorn
    # Use standard 8000 for local; Render uses the $PORT env var
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)