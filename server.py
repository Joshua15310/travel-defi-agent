from __future__ import annotations

import json
import os
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRouter

import agent as agent_module
from agent import AIMessage, HumanMessage, SystemMessage


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
# Data model (what we store == what we stream == what history returns)
# -----------------------------------------------------------------------------

Role = Literal["user", "ai", "assistant", "system"]
MsgType = Literal["human", "ai", "system"]

def _new_msg(role: Role, content: str) -> Dict[str, Any]:
    role_norm: Role = role if role in ("user", "ai", "assistant", "system") else "user"
    
    # Normalize role to what AgentChat expects
    if role_norm in ("ai", "assistant"):
        final_role = "assistant"
        msg_type = "ai"
    elif role_norm == "system":
        final_role = "system"
        msg_type = "system"
    else:
        final_role = "user"
        msg_type = "human"

    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": msg_type,
        "role": final_role,  # AgentChat expects: user, assistant, or system
        "content": content or "",
    }


# In-memory stores (reset on redeploy)
THREADS: Dict[str, List[Dict[str, Any]]] = {}
LAST_ERROR: Dict[str, Any] = {}
LAST_STREAM: List[Dict[str, Any]] = []


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _record(event: str, data: Any) -> None:
    LAST_STREAM.append({"time": _now(), "event": event, "data": data})
    if len(LAST_STREAM) > 250:
        del LAST_STREAM[:100]


def _normalize_incoming_messages(messages: Any) -> List[Dict[str, Any]]:
    """
    AgentChat sends input.messages like: [{role, content, ...}, ...]
    We convert them into the same canonical ChatMessage objects we store/stream.
    """
    if not isinstance(messages, list):
        return []
    out: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        content = str(m.get("content", ""))
        out.append(_new_msg(role=role, content=content))
    return out


def _to_langchain_messages(history: List[Dict[str, Any]]):
    lc = []
    for m in history:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            lc.append(SystemMessage(content=content))
        elif role == "ai":
            lc.append(AIMessage(content=content))
        else:
            lc.append(HumanMessage(content=content))
    return lc


def _extract_ai_text(result: Any) -> str:
    if isinstance(result, dict):
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if hasattr(last, "content"):
                return str(last.content)
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]
    return "I'm here—tell me what you want to book."


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
            "request_body": body if DEBUG else {"note": "Set DEBUG=1 to include request body"},
        }
    )


def _assistant_catalog() -> Dict[str, Any]:
    return {
        "agents": [
            {
                "id": "travel-defi-agent",
                "name": "Travel DeFi Agent",
                "description": "Books flights and hotels using USDC",
            }
        ]
    }


def _info_payload() -> Dict[str, Any]:
    return {
        "graphs": {
            "agent": {
                "input_schema": {"messages": "list"},
                "output_schema": {"result": "json"},
            }
        }
    }


# -----------------------------------------------------------------------------
# Root endpoints
# -----------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok"}


@app.head("/")
def root_head():
    return JSONResponse(content=None, status_code=200)


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
    incoming = _normalize_incoming_messages(body.get("messages", []))
    if not incoming:
        return JSONResponse(status_code=400, content={"error": "Missing messages"})

    thread_id = "cto-run"
    THREADS[thread_id] = incoming
    reply = _call_agent(thread_id)
    THREADS[thread_id].append(_new_msg("ai", reply))

    return {"result": {"reply": reply}}


# -----------------------------------------------------------------------------
# /agent endpoints (AgentChat)
# -----------------------------------------------------------------------------

agent = APIRouter(prefix="/agent")


@agent.get("/info")
def agent_info():
    return _info_payload()


@agent.get("/assistants/search")
def agent_assistants_search():
    return _assistant_catalog()


@agent.post("/threads")
def create_thread():
    tid = str(uuid.uuid4())
    THREADS[tid] = []
    return {"thread_id": tid}


@agent.post("/threads/search")
def threads_search():
    return [{"thread_id": t} for t in THREADS.keys()]


# IMPORTANT: support BOTH GET and POST (different AgentChat builds use different methods)
@agent.get("/threads/{thread_id}/history")
def thread_history_get(thread_id: str):
    return THREADS.get(thread_id, [])


@agent.post("/threads/{thread_id}/history")
def thread_history_post(thread_id: str):
    return THREADS.get(thread_id, [])


@agent.get("/debug/last_error")
def debug_last_error():
    return LAST_ERROR or {"ok": True}


@agent.get("/debug/last_stream")
def debug_last_stream():
    return {"count": len(LAST_STREAM), "last": LAST_STREAM[-80:]}


@agent.get("/debug/threads")
def debug_threads():
    """Debug endpoint to see all thread data"""
    return {
        "threads": {
            tid: {
                "message_count": len(msgs),
                "messages": msgs
            } for tid, msgs in THREADS.items()
        }
    }


@agent.post("/threads/{thread_id}/runs/stream")
async def runs_stream(thread_id: str, request: Request):
    body = await request.json()
    incoming = _normalize_incoming_messages((body.get("input") or {}).get("messages", []))

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    run_id = str(uuid.uuid4())

    async def gen():
        LAST_STREAM.clear()
        try:
            # 1. Send metadata event (SSE format)
            meta = {
                "run_id": run_id,
                "thread_id": thread_id,
                "assistant_id": body.get("assistant_id"),
                "status": "running",
            }
            _record("metadata", meta)
            yield f"event: metadata\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

            # 2. Call agent
            reply = _call_agent(thread_id)
            ai_msg = _new_msg("assistant", reply)  # Use "assistant" instead of "ai"
            THREADS[thread_id].append(ai_msg)

            # 3. Stream the message in chunks (simulate streaming)
            # First send the message structure
            _record("messages/partial", ai_msg)
            yield f"event: messages/partial\ndata: {json.dumps([ai_msg], ensure_ascii=False)}\n\n"

            # 4. Then confirm with final messages event
            _record("messages", [ai_msg])
            yield f"event: messages\ndata: {json.dumps([ai_msg], ensure_ascii=False)}\n\n"

            # 5. Send end event with success status
            end = {
                "run_id": run_id,
                "status": "success"
            }
            _record("end", end)
            yield f"event: end\ndata: {json.dumps(end, ensure_ascii=False)}\n\n"

        except Exception as e:
            _capture_error(thread_id, run_id, body, e)
            err = {
                "run_id": run_id,
                "error": LAST_ERROR.get("error", "unknown error")
            }
            _record("error", err)
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"

            end = {
                "run_id": run_id,
                "status": "error"
            }
            _record("end", end)
            yield f"event: end\ndata: {json.dumps(end, ensure_ascii=False)}\n\n"

    # Use StreamingResponse with proper SSE headers
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


app.include_router(agent)