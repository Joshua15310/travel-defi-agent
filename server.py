import os
import uuid
import json
import traceback
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
    allow_origins=["https://agentchat.vercel.app", "http://localhost:3000", "*"],
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

@app.post("/agent/threads/search")
async def search_threads(request: Request):
    return []

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

# --- SERIALIZATION HELPER ---
class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'model_dump'):
            return obj.model_dump()
        if hasattr(obj, 'dict'):
            return obj.dict()
        if hasattr(obj, 'json'):
            return obj.json()
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)

# 7. Streaming Run Endpoint (FIXED FOR VERCEL PROTOCOL)
@app.post("/agent/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request):
    try:
        body = await request.json()
        input_data = body.get("input", {})
        config = {"configurable": {"thread_id": thread_id}}

        async def event_generator():
            try:
                # CRITICAL FIX: We must use stream_mode="values" 
                # This ensures Vercel gets the full state (messages list), not just a node update.
                async for event in graph.astream(input_data, config=config, stream_mode="values"):
                    yield {
                        "event": "values", # The Vercel app specifically listens for this event type
                        "data": json.dumps(event, cls=CustomEncoder)
                    }
                yield {"event": "end"}
            except Exception as e:
                print(f"Stream error: {e}")
                yield {
                    "event": "error",
                    "data": json.dumps({"error": str(e)})
                }
                yield {"event": "end"}

        return EventSourceResponse(event_generator())
    
    except Exception as e:
        return {"error": str(e)}

# 8. Mock History Endpoint
@app.post("/agent/threads/{thread_id}/history")
async def post_thread_history(thread_id: str, request: Request):
    return []

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)