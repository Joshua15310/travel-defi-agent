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
    # This specifically names the field 'input' which the Playground UI 
    # uses for the main text box in many versions.
    input: str = Field(..., description="Your booking request (e.g., 'Hotel in Rome')")
    messages: List[Dict[str, Any]] = Field(default=[], description="Chat history")

def input_adapter(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures the agent always gets the user query from the UI."""
    # 1. Capture the direct input text
    query = input_data.get("input", "").strip()
    
    # 2. Capture message history if it exists
    raw_messages = input_data.get("messages", [])
    converted = []
    for m in raw_messages:
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if content.strip():
            converted.append(HumanMessage(content=content))

    # 3. If the history is empty but we have an 'input' field, use that
    if not converted and query:
        converted.append(HumanMessage(content=query))

    # 4. Return the full state with a guaranteed query
    return {
        "messages": converted,
        "user_query": query,
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
# We explicitly set playground_type="chat" here
clean_agent = RunnableLambda(input_adapter).with_types(input_type=SimpleInput) | graph

# 4. Add Routes - Set to "default" to fix the error in Screenshot 301
add_routes(
    app,
    clean_agent,
    path="/agent",
    playground_type="default" # <--- Change this from "chat" to "default"
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)