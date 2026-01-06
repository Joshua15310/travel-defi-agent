import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from agent import workflow_app

# --- 1. DEFINE APP ---
app = FastAPI(
    title="Crypto Travel Agent",
    version="1.0",
    description="A LangGraph agent interface"
)

# --- 2. SECURITY HEADERS (CORS) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. ROUTES ---
# The Playground
add_routes(app, workflow_app, path="/agent")

# The Chat Interface (for index.html)
@app.post("/chat")
async def chat_endpoint(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message")
        thread_id = data.get("thread_id", "default_thread")

        if not user_message:
            return JSONResponse({"error": "No message provided"}, status_code=400)

        response = await workflow_app.ainvoke(
            input={"messages": [{"content": user_message, "type": "human"}]},
            config={"configurable": {"thread_id": thread_id}}
        )

        last_message = response["messages"][-1].content
        return JSONResponse({"reply": last_message})

    except Exception as e:
        print(f"Error in /chat: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# Root Redirect (Forces the Trailing Slash)
@app.get("/")
async def redirect_root_to_playground():
    return RedirectResponse(url="/agent/playground/") 

# --- 4. EXECUTION ENTRY POINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    
    # We print this to the logs to PROVE the fix is active
    print(f"🚀 Starting Server on Port {port} with Proxy Headers ENABLED")
    
    uvicorn.run(
        "server:app", 
        host="0.0.0.0", 
        port=port, 
        # HARDCODED SECURITY FIXES:
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )