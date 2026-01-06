import os
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from agent import workflow_app

app = FastAPI(title="Crypto Travel Agent")

# 1. Enable CORS (Allows your local HTML file to talk to the server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. STANDARD ROUTE: LangServe (Keep this for the Playground)
add_routes(app, workflow_app, path="/agent")

# 3. CUSTOM ROUTE: The "Silver Bullet" Fix 🔫
# This endpoint accepts simple JSON and handles the complex config on the server.
@app.post("/chat")
async def chat_endpoint(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message")
        thread_id = data.get("thread_id", "default_thread")

        if not user_message:
            return JSONResponse({"error": "No message provided"}, status_code=400)

        # We construct the complex LangGraph payload HERE, on the server.
        # This guarantees the format is 100% correct every time.
        response = await workflow_app.ainvoke(
            input={"messages": [{"content": user_message, "type": "human"}]},
            config={"configurable": {"thread_id": thread_id}}
        )

        # Extract just the last message to send back to the UI
        last_message = response["messages"][-1].content
        return JSONResponse({"reply": last_message})

    except Exception as e:
        print(f"Error in /chat: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# 4. Root Redirect
@app.get("/")
async def redirect_root_to_playground():
    return RedirectResponse(url="/agent/playground")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)