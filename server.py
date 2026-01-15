from typing import Any, Dict, List, Optional
import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from fastapi.routing import APIRouter

app = FastAPI()

# CORS: allow agentchat.vercel.app to call your Render API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://agentchat.vercel.app", "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Root endpoints (CTO / program checks)
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
                "description": "Books hotels using USDC and DeFi protocols",
            }
        ]
    }

@app.get("/info")
def info():
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list of {role, content}"},
                "output_schema": {"result": "structured JSON"},
            }
        }
    }

@app.post("/run")
async def run_agent(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    if not isinstance(messages, list) or len(messages) == 0:
        return JSONResponse(status_code=400, content={"error": "Missing messages (expected a list)"})

    first = messages[0] if isinstance(messages[0], dict) else {}
    user_text = first.get("content", "")

    return {
        "result": {
            "reply": f"Agent received: {user_text}",
            "status": "success",
        }
    }

# -------------------------
# /agent endpoints (AgentChat / LangGraph-style)
# -------------------------

agent = APIRouter(prefix="/agent")

@agent.get("/info")
def agent_info():
    return info()

@agent.get("/assistants/search")
def agent_assistants_search():
    # Some AgentChat builds call /agent/assistants/search (not the root one)
    return assistants_search()

@agent.post("/run")
async def agent_run(request: Request):
    # AgentChat sometimes calls /agent/run
    return await run_agent(request)

# IMPORTANT:
# AgentChat expects ARRAYS here (so it can do .forEach safely)

@agent.post("/threads/search")
async def agent_threads_search() -> List[Dict[str, Any]]:
    # must be a list
    return []

@agent.post("/threads")
async def agent_threads_create() -> Dict[str, Any]:
    # minimal thread object that won't crash the UI
    return {
        "thread_id": "demo-thread",
        "metadata": {},
    }

@agent.post("/threads/{thread_id}/history")
async def agent_thread_history(thread_id: str) -> List[Dict[str, Any]]:
    # must be a list of messages
    return []

@agent.post("/threads/{thread_id}/runs/stream")
async def agent_runs_stream(thread_id: str, request: Request):
    async def gen():
        payload = {
            "messages": [
                {
                    "type": "ai",
                    "content": "Welcome to Warden Travel! What would you like to book today?"
                }
            ],
            "requirements_complete": False
        }
        yield {
            "event": "values",
            "data": json.dumps(payload)
        }
        yield {
            "event": "end",
            "data": json.dumps({"status": "complete"})
        }

    return EventSourceResponse(gen())


app.include_router(agent)

# Nice-to-have: stop HEAD / showing 404 in Render health checks
@app.get("/")
def root():
    return {"status": "ok"}
