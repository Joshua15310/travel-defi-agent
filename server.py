from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from sse_starlette.sse import EventSourceResponse

import agent as agent_module
from agent import HumanMessage, AIMessage, SystemMessage  # these are in your exports


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://agentchat.vercel.app", "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

THREADS: Dict[str, List[Dict[str, str]]] = {}


def _normalize_messages(messages: Any) -> List[Dict[str, str]]:
    if not isinstance(messages, list):
        return []
    out: List[Dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "")).strip() or "user"
        content = str(m.get("content", "")).strip()
        out.append({"role": role, "content": content})
    return out


def _to_langchain_messages(history: List[Dict[str, str]]):
    lc = []
    for m in history:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            lc.append(SystemMessage(content=content))
        elif role == "ai" or role == "assistant":
            lc.append(AIMessage(content=content))
        else:
            lc.append(HumanMessage(content=content))
    return lc


def _extract_ai_text(result: Any) -> str:
    # LangGraph often returns dict with "messages"
    if isinstance(result, dict):
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            # last can be a LangChain message object
            if hasattr(last, "content"):
                return str(last.content)
            # or dict
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]
    # fallback
    return "I’m here—tell me what you want to book (destination, dates, budget)."


def _assistant_catalog() -> Dict[str, Any]:
    return {
        "agents": [
            {
                "id": "travel-defi-agent",
                "name": "Travel DeFi Agent",
                "description": "Books hotels using USDC and DeFi protocols",
            }
        ]
    }


def _info_payload() -> Dict[str, Any]:
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list of {role, content}"},
                "output_schema": {"result": "structured JSON"},
            }
        }
    }


def _call_agent(thread_id: str) -> str:
    if not hasattr(agent_module, "app"):
        raise RuntimeError("agent.py does not export `app`. Add: app = workflow.compile(checkpointer=memory)")

    lc_history = _to_langchain_messages(THREADS.get(thread_id, []))

    # IMPORTANT: config thread_id enables per-thread memory with the checkpointer
    result = agent_module.app.invoke(
        {"messages": lc_history},
        config={"configurable": {"thread_id": thread_id}},
    )
    return _extract_ai_text(result)


# -------------------------
# Root endpoints (CTO)
# -------------------------

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/status")
def status():
    return {"status": "ok"}

@app.get("/assistants/search")
def assistants_search():
    return _assistant_catalog()

@app.get("/info")
def info():
    return _info_payload()

@app.post("/run")
async def run(request: Request):
    body = await request.json()
    messages = _normalize_messages(body.get("messages", []))
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Missing messages (expected list of {role, content})"})

    temp_thread = "cto-run"
    THREADS[temp_thread] = messages
    reply = _call_agent(temp_thread)

    return {"result": {"reply": reply, "status": "success"}}


# -------------------------
# /agent endpoints (AgentChat)
# -------------------------

agent = APIRouter(prefix="/agent")

@agent.get("/info")
def agent_info():
    return _info_payload()

@agent.get("/assistants/search")
def agent_assistants_search():
    return _assistant_catalog()

@agent.post("/threads")
def create_thread():
    thread_id = str(uuid.uuid4())
    THREADS[thread_id] = []
    return {"thread_id": thread_id}

@agent.post("/threads/search")
def threads_search():
    return [{"thread_id": tid} for tid in THREADS.keys()]

@agent.post("/threads/{thread_id}/history")
def thread_history(thread_id: str):
    return THREADS.get(thread_id, [])

@agent.post("/threads/{thread_id}/runs/stream")
async def runs_stream(thread_id: str, request: Request):
    body = await request.json()
    incoming = _normalize_messages((body.get("input") or {}).get("messages", []))

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    async def gen():
        try:
            reply = _call_agent(thread_id)
            THREADS[thread_id].append({"role": "ai", "content": reply})

            yield {"event": "values", "data": json.dumps({"messages": [{"role": "ai", "content": reply}]})}
            yield {"event": "end", "data": json.dumps({"status": "complete"})}
        except Exception as e:
            msg = f"Server error: {type(e).__name__}: {e}"
            THREADS[thread_id].append({"role": "ai", "content": msg})
            yield {"event": "values", "data": json.dumps({"messages": [{"role": "ai", "content": msg}]})}
            yield {"event": "end", "data": json.dumps({"status": "complete"})}

    return EventSourceResponse(gen())

app.include_router(agent)
