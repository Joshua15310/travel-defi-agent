from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from agent import workflow_app as graph

app = FastAPI(
    title="Travel DeFi Agent",
    version="1.0",
    description="A LangGraph agent hosted on Render"
)

# 1. CORS - ALLOW VERCEL (Critical)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. THE WELCOME MAT (Fixes the "404" Error)
# When Vercel pings the root "/", we say "Yes, I'm here!"
@app.get("/")
def redirect_to_docs():
    return {"status": "Travel Agent is Live", "service": "LangGraph"}

# 3. HELPER: Convert Vercel's simple JSON to LangChain Objects
def convert_messages(messages):
    converted = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "user":
                converted.append(HumanMessage(content=content))
            elif role == "assistant":
                converted.append(AIMessage(content=content))
            else:
                converted.append(SystemMessage(content=content))
        else:
            converted.append(m)
    return converted

# 4. THE ADAPTER (Fixes the "Missing Form Fields" Crash)
def input_adapter(input_data: dict) -> dict:
    raw_messages = input_data.get("messages", [])
    safe_messages = convert_messages(raw_messages)
    
    return {
        "messages": safe_messages,
        # Default values to satisfy the agent's strict requirements
        "destination": "unknown", 
        "budget_usd": 0.0,
        "hotel_name": "none",
        "hotel_price": 0.0,
        "needs_swap": False,
        "swap_amount": 0.0
    }

# 5. Connect the pieces
compatible_model = RunnableLambda(input_adapter) | graph

# 6. Add Routes at "/agent"
add_routes(
    app,
    compatible_model,
    path="/agent",
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)