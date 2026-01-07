import os
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes

# Import 'workflow_app' from agent.py
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

# --- VERCEL COMPATIBILITY LAYER ---

# 3. Info Endpoint (Discovery)
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

# 4. Mock Search Endpoint (History)
@app.post("/agent/threads/search")
async def search_threads(request: Request):
    return []

# 5. NEW FIX: Mock Thread Creation (This fixes the 404 error)
@app.post("/agent/threads")
async def create_thread(request: Request):
    return {
        "thread_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
        "status": "idle",
        "config": {},
        "values": None
    }

# 6. Mock Get Thread (Prevents potential future errors)
@app.get("/agent/threads/{thread_id}")
async def get_thread(thread_id: str):
    return {
        "thread_id": thread_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
        "status": "idle",
        "values": None
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)