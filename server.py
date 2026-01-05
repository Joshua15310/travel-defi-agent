from fastapi import FastAPI, Request
# FIX: Import 'workflow_app' but rename it to 'graph' for this file
from agent import workflow_app as graph 

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "Travel DeFi Agent is running"}

@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    user_input = data.get("message", "")
    
    # Run the agent
    # We use 'graph' here because we aliased it in the import above
    result = await graph.ainvoke({"messages": [("user", user_input)]})
    
    # Extract the last message content
    # This logic assumes your agent adds the final response to the 'messages' list
    last_message = result["messages"][-1].content
    
    return {"response": last_message}