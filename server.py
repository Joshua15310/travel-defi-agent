from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from agent import workflow_app as graph

app = FastAPI(
    title="Travel DeFi Agent",
    version="1.0",
    description="A LangGraph agent hosted on Render"
)

# 1. CORS - Allow Vercel to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. THE TRANSLATOR (Fixes the Vercel Error)
# This function automatically fills in the missing "Form Fields" 
# that the Vercel tester doesn't know about.
def input_adapter(input_data: dict) -> dict:
    # Get the messages (or empty list if missing)
    messages = input_data.get("messages", [])
    
    # Return the FULL state structure your agent demands
    return {
        "messages": messages,
        "user_query": messages[-1].content if messages else "",
        # Fill required fields with safe defaults so the agent doesn't crash
        "destination": "unknown", 
        "budget_usd": 0.0,
        "hotel_name": "none",
        "hotel_price": 0.0,
        "needs_swap": False,
        "swap_amount": 0.0
    }

# Chain the translator to your graph
# Logic: Receive Simple Input -> Add Defaults -> Run Agent
compatible_model = RunnableLambda(input_adapter) | graph

# 3. Add Routes
add_routes(
    app,
    compatible_model, # Use the compatible model, not the raw graph
    path="/agent",
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)