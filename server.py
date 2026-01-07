import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes

# Import 'workflow_app' from agent.py (The "Factory")
from agent import workflow_app as graph 

app = FastAPI(
    title="Nomad AI Travel Agent",
    version="1.0",
    description="Warden Protocol Travel Agent",
)

# 1. Broaden CORS for Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 2. Routes for the Agent
add_routes(
    app,
    graph,
    path="/agent",
)

# 3. FIX FOR VERCEL: Info Endpoint (Discovery)
@app.get("/agent/info")
async def get_info():
    return {
        "graphs": {
            "agent": {
                "input_schema": graph.input_schema.schema(),
                "output_schema": graph.output_schema.schema(),
            }
        }
    }

# 4. NEW FIX: Mock Search Endpoint (History)
# This stops the "404 /threads/search" error in the Vercel console
@app.post("/agent/threads/search")
async def search_threads(request: Request):
    # We return an empty list to tell Vercel "No past history found"
    # This allows the UI to load without crashing.
    return []

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)