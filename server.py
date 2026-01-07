import os
import uuid
import json
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
from sse_starlette.sse import EventSourceResponse

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

# 2. Standard LangServe Routes
add_routes(
    app,
    graph,
    path="/agent",
)

# --- VERCEL COMPATIBILITY LAYER ---

# 3. Info Endpoint
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

# 4. Mock Search Endpoint
@app.post("/agent/threads/search")
async def search_threads(request: Request):
    return []

# 5. Mock Thread Creation
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

# 6. Mock Get Thread
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

# 7. Streaming Run Endpoint
@app.post("/agent/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request):
    body = await request.json()
    input_data = body.get("input", {})
    config = {"configurable": {"thread_id": thread_id}}

    async def event_generator():
        async for event in graph.astream(input_data, config=config):
            yield {
                "event": "data",
                "data": json.dumps(event)
            }
        yield {"event": "end"}

    return EventSourceResponse(event_generator())

# 8. CORRECTED: Mock History Endpoint
# Changed return value from {"messages": []} to just []
@app.post("/agent/threads/{thread_id}/history")
async def post_thread_history(thread_id: str, request: Request):
    return []

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)