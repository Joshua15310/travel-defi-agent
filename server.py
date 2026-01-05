from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from agent import workflow_app as graph

app = FastAPI()

# 1. ADD CORS PERMISSIONS (Critical for the Vercel Tester)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

@app.get("/")
def read_root():
    return {"status": "Travel DeFi Agent is running"}

@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    
    # 2. HANDLE INPUT FLEXIBILITY
    # The Tester sends "messages" (list), but simple tests send "message" (string)
    if "messages" in data:
        # If the tester sends a list of messages, use it directly
        user_input = data["messages"]
    else:
        # If it's a simple test, format it as a user message
        user_input = [("user", data.get("message", ""))]
    
    # Run the agent
    result = await graph.ainvoke({"messages": user_input})
    
    # Extract the last response
    last_message = result["messages"][-1].content
    
    # 3. RETURN FORMAT
    # Return in a format the Tester likely understands (JSON with 'messages')
    return {
        "messages": [
            {"type": "ai", "content": last_message}
        ],
        "response": last_message # Keep this for backward compatibility
    }