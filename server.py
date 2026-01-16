from __future__ import annotations

import json
import os
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from sse_starlette.sse import EventSourceResponse

import agent as agent_module
from agent import AIMessage, HumanMessage, SystemMessage


# -----------------------------------------------------------------------------
# App + CORS
# -----------------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://agentchat.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "*",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

DEBUG = os.getenv("DEBUG", "0") == "1"


# -----------------------------------------------------------------------------
# In-memory stores
# -----------------------------------------------------------------------------

THREADS: Dict[str, List[Dict[str, str]]] = {}
LAST_ERROR: Dict[str, Any] = {}
LAST_STREAM: List[Dict[str, Any]] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _normalize_messages(messages: Any) -> List[Dict[str, str]]:
    if not isinstance(messages, list):
        return []
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                }
            )
    return out


def _to_langchain_messages(history: List[Dict[str, str]]):
    lc = []
    for m in history:
        if m["role"] == "system":
            lc.append(SystemMessage(content=m["content"]))
        elif m["role"] in ("ai", "assistant"):
            lc.append(AIMessage(content=m["content"]))
        else:
            lc.append(HumanMessage(content=m["content"]))
    return lc


def _extract_ai_text(result: Any) -> str:
    if isinstance(result, dict):
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if hasattr(last, "content"):
                return str(last.content)
    return "I’m here—tell me what you want to book."


def _call_agent(thread_id: str) -> str:
    if not hasattr(agent_module, "app"):
        raise RuntimeError(
            "agent.py must export:\n"
            "app = workflow_app\n"
            "graph = workflow_app"
        )

    lc_history = _to_langchain_messages(THREADS.get(thread_id, []))
    result = agent_module.app.invoke(
        {"messages": lc_history},
        config={"configurable": {"thread_id": thread_id}},
    )
    return _extract_ai_text(result)


def _capture_error(thread_id: str, run_id: str, body: Any, e: Exception):
    LAST_ERROR.clear()
    LAST_ERROR.update(
        {
            "time": _now(),
            "thread_id": thread_id,
            "run_id": run_id,
            "error": f"{type(e).__name__}: {str(e)}",
            "traceback": traceback.format_exc(),
            "request_body": body if DEBUG else "Enable DEBUG=1 to see request body",
        }
    )


# -----------------------------------------------------------------------------
# Root endpoints (CTO)
# -----------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok"}


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
                "description": "Books flights and hotels using USDC",
            }
        ]
    }


@app.get("/info")
def info():
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list"},
                "output_schema": {"result": "json"},
            }
        }
    }


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    messages = _normalize_messages(body.get("messages", []))
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Missing messages"})

    THREADS["cto-run"] = messages
    reply = _call_agent("cto-run")
    return {"result": {"reply": reply}}


# -----------------------------------------------------------------------------
# /agent endpoints (AgentChat)
# -----------------------------------------------------------------------------

agent = APIRouter(prefix="/agent")


@agent.get("/info")
def agent_info():
    return info()


@agent.get("/assistants/search")
def agent_assistants_search():
    return assistants_search()


@agent.post("/threads")
def create_thread():
    tid = str(uuid.uuid4())
    THREADS[tid] = []
    return {"thread_id": tid}


@agent.post("/threads/search")
def threads_search():
    return [{"thread_id": t} for t in THREADS.keys()]


@agent.post("/threads/{thread_id}/history")
def thread_history(thread_id: str):
    return THREADS.get(thread_id, [])


@agent.get("/debug/last_error")
def debug_last_error():
    return LAST_ERROR or {"ok": True}


@agent.get("/debug/last_stream")
def debug_last_stream():
    return LAST_STREAM


@agent.post("/threads/{thread_id}/runs/stream")
async def runs_stream(thread_id: str, request: Request):
    body = await request.json()
    incoming = _normalize_messages((body.get("input") or {}).get("messages", []))

    THREADS.setdefault(thread_id, [])
    THREADS[thread_id].extend(incoming)

    run_id = str(uuid.uuid4())

    async def gen():
        try:
            # metadata
            meta = {
                "run_id": run_id,
                "thread_id": thread_id,
                "assistant_id": body.get("assistant_id"),
                "status": "running",
            }
            LAST_STREAM.clear()
            LAST_STREAM.append({"event": "metadata", "data": meta})
            yield {"event": "metadata", "data": json.dumps(meta)}

            # message
            reply = _call_agent(thread_id)
            THREADS[thread_id].append({"role": "ai", "content": reply})

            msg = {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "ai",
                "role": "ai",
                "content": reply,
            }
            LAST_STREAM.append({"event": "messages", "data": [msg]})
            yield {"event": "messages", "data": json.dumps([msg])}

            # keepalive ping
            ping = {"run_id": run_id, "ok": True}
            LAST_STREAM.append({"event": "ping", "data": ping})
            yield {"event": "ping", "data": json.dumps(ping)}

            # ❌ NO `end` EVENT — THIS IS THE FIX

        except Exception as e:
            _capture_error(thread_id, run_id, body, e)
            err = {"run_id": run_id, "error": LAST_ERROR["error"]}
            yield {"event": "error", "data": json.dumps(err)}

    return EventSourceResponse(
        gen(),
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


app.include_router(agent)
