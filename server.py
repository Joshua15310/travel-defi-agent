from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from fastapi.routing import APIRouter
import json
import uuid

# Import your agent logic
from agent import run_agent_logic  # assumes you have a function like this in agent.py

app = FastAPI()

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You can restrict to agentchat.vercel.app if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory thread store
THREADS = {}

# -------------------------
# Root endpoints (CTO checks)
# -------------------------

@app.get("/status")
def status():
    return {"status": "ok"}

@app.get("/assistants/search")
def assistants_search():
    return {
        "agents": [
            {
                "id": "travel-defi-agent",
                "name": "Travel DeFi Agent",
                "description": "Books hotels using USDC and DeFi protocols"
            }
        ]
    }

@app.get("/info")
def info():
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list of {role, content}"},
                "output_schema": {"result": "structured JSON"}
            }
        }
    }

@app.post("/run")
async def run(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Missing messages"})
    reply = run_agent_logic(messages)
    return {"result": {"reply": reply, "status": "success"}}

# -------------------------
# /agent endpoints (AgentChat)
# -------------------------

agent = APIRouter(prefix="/agent")

@agent.get("/info")
def agent_info():
    return info()

@agent.get("/assistants/search")
def agent_assistants():
    return assistants_search()

@agent.post("/run")
async def agent_run(request: Request):
    return await run(request)

@agent.post("/threads")
def create_thread():
    thread_id = str(uuid.uuid4())
    THREADS[thread_id] = []
    return {"thread_id": thread_id}

@agent.post("/threads/search")
def search_threads():
    return [{"thread_id": tid} for tid in THREADS.keys()]

@agent.post("/threads/{thread_id}/history")
def thread_history(thread_id: str):
    return THREADS.get(thread_id, [])

@agent.post("/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request):
    body = await request.json()
    input_messages = body.get("input", {}).get("messages", [])

    # Save incoming messages
    THREADS.setdefault(thread_id, []).extend(input_messages)

    # Generate reply using your agent logic
    reply = run_agent_logic(THREADS[thread_id])

    # Save reply
    THREADS[thread_id].append({"role": "ai", "content": reply})

    async def gen():
        yield {
            "event": "values",
            "data": json.dumps({
                "messages": [{"role": "ai", "content": reply}],
                "requirements_complete": False
            })
        }
        yield {
            "event": "end",
            "data": json.dumps({"status": "complete"})
        }

    return EventSourceResponse(gen())

app.include_router(agent)

@app.get("/")
def root():
    return {"status": "ok"}
