import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from agent import workflow_app

app = FastAPI(title="Crypto Travel Agent")

# 1. Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Add LangServe Routes (Playground)
add_routes(app, workflow_app, path="/agent")

# 3. Custom Chat Endpoint (For index.html)
@app.post("/chat")
async def chat_endpoint(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message")
        thread_id = data.get("thread_id", "default_thread")

        if not user_message:
            return JSONResponse({"error": "No message provided"}, status_code=400)

        # Run the agent
        response = await workflow_app.ainvoke(
            input={"messages": [{"content": user_message, "type": "human"}]},
            config={"configurable": {"thread_id": thread_id}}
        )

        # Return just the last message
        last_message = response["messages"][-1].content
        return JSONResponse({"reply": last_message})

    except Exception as e:
        print(f"Error in /chat: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# 4. Root Redirect (Points to Playground)
@app.get("/")
async def redirect_root_to_playground():
    return RedirectResponse(url="/agent/playground/") 

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # --- CRITICAL FIX FOR RENDER ---
    # proxy_headers=True: Tells Uvicorn to trust that Render is handling HTTPS.
    # forwarded_allow_ips="*": Accepts headers from Render's load balancer.
    # This prevents the "White Page" / Mixed Content errors.
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")