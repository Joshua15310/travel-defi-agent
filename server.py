from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
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

# --- 2. INPUT SIMPLIFIER ---

# This model tells the UI: "The User ONLY needs to provide messages."
class SimpleInput(BaseModel):
    messages: List[Dict[str, Any]] = Field(
        ..., 
        description="Chat history. Example: [{'role': 'user', 'content': 'Book a hotel in Paris'}]"
    )

# This function takes that simple input and fills in all the blank fields for the agent
def input_adapter(input_data: Dict[str, Any]) -> Dict[str, Any]:
    # 1. Extract messages
    # Handle both object style (input_data.messages) and dict style (input_data['messages'])
    raw_messages = input_data.get("messages", [])
    
    # 2. Convert to LangChain objects if needed
    converted = []
    for m in raw_messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
            if m.get("type") == "human" or role == "user":
                converted.append(HumanMessage(content=content))
            elif m.get("type") == "ai" or role == "assistant":
                converted.append(AIMessage(content=content))
            else:
                converted.append(HumanMessage(content=content)) # Fallback
        else:
            converted.append(m)

    # 3. Return the FULL state with dummy values filled in automatically
    return {
        "messages": converted,
        "user_query": "",
        "destination": "unknown", 
        "budget_usd": 0.0,
        "hotel_name": "none",
        "hotel_price": 0.0,
        "needs_swap": False,
        "swap_amount": 0.0,
        "final_status": "started",
        "tx_hash": "none"
    }

# 4. Create the "Polished" Model
# We verify the input type so the Playground looks clean
clean_agent = RunnableLambda(input_adapter).with_types(input_type=SimpleInput) | graph

# 5. Add Routes
add_routes(
    app,
    clean_agent,
    path="/agent",
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)