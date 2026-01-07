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

# 1. CORS FIX: Allow Vercel specifically + wildcard fallback
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
            return str(obj) # Fallback to string if all else fails

# 7. Streaming Run Endpoint (WITH DEBUG LOGGING)
@app.post("/agent/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request):
    try:
        # DEBUG LOG: Confirm request received
        print(f"--- [DEBUG] Stream Request Received for Thread: {thread_id} ---")
        
        body = await request.json()
        print(f"--- [DEBUG] Request Body: {json.dumps(body)} ---")
        
        input_data = body.get("input", {})
        config = {"configurable": {"thread_id": thread_id}}

        async def event_generator():
            try:
                print("--- [DEBUG] Starting LangGraph Stream ---")
                async for event in graph.astream(input_data, config=config):
                    print(f"--- [DEBUG] Event Yielded: {type(event)} ---")
                    yield {
                        "event": "data",
                        "data": json.dumps(event, cls=CustomEncoder)
                    }
                print("--- [DEBUG] Stream Finished ---")
                yield {"event": "end"}
            except Exception as e:
                print(f"--- [ERROR] Stream Crashed: {e} ---")
                traceback.print_exc()
                # Send error to UI so it's not blank
                yield {
                    "event": "data",
                    "data": json.dumps({"error": str(e)})
                }
                yield {"event": "end"}

        return EventSourceResponse(event_generator())
    
    except Exception as e:
        print(f"--- [FATAL ERROR] Endpoint Failed: {e} ---")
        return {"error": str(e)}

# 8. Mock History Endpoint
@app.post("/agent/threads/{thread_id}/history")
async def post_thread_history(thread_id: str, request: Request):
    return []

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)