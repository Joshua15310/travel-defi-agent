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
    allow_credentials=False,  # keep False with wildcard
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
LAST_STREAM: List[Dict[str, Any]] = []
LAST_SNAPSHOT: Dict[str, Any] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _record_stream(event: str, data: Any):
    LAST_STREAM.append({"time": _now(), "event": event, "data": data})
    if len(LAST_STREAM) > 250:
        del LAST_STREAM[:100]


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
        elif role in ("ai", "assistant"):
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
    """
    Requires agent.py to export:
      app = workflow_app
      graph = workflow_app
    """
    if not hasattr(agent_module, "app"):
        raise RuntimeError(
            "agent.py does not export `app`. Add at bottom of agent.py:\n"
            "app = workflow_app\n"
            "graph = workflow_app\n"
        )

    lc_history = _to_langchain_messages(THREADS.get(thread_id, []))

    result = agent_module.app.invoke(
        {"messages": lc_history},
        config={"configurable": {"thread_id": thread_id}},
    )
    return _extract_ai_text(result)


def _capture_error(thread_id: str, run_id: str, request_body: Any, e: Exception):
    tb = traceback.format_exc()
    LAST_ERROR.clear()
    LAST_ERROR.update(
        {
            "time": _now(),
            "thread_id": thread_id,
            "run_id": run_id,
            "error": f"{type(e).__name__}: {str(e)}",
            "traceback": tb,
            "request_body": request_body if DEBUG else {"note": "Set DEBUG=1 to include request body"},
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

    thread_id = "cto-run"
    THREADS[thread_id] = messages
    reply = _call_agent(thread_id)
    return {"result": {"reply": reply, "status": "success"}}


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
    thread_id = str(uuid.uuid4())
    THREADS[thread_id] = []
    return {"thread_id": thread_id}


@agent.post("/threads/search")
def threads_search():
    return [{"thread_id": tid} for tid in THREADS.keys()]


@agent.post("/threads/{thread_id}/history")
def thread_history(thread_id: str):
    return THREADS.get(thread_id, [])


@agent.get("/debug/last_error")
def debug_last_error():
    return LAST_ERROR or {"ok": True, "message": "No errors captured yet."}


@agent.get("/debug/last_stream")
def debug_last_stream():
    return {"count": len(LAST_STREAM), "last": LAST_STREAM[-80:]}


@agent.get("/debug/snapshot")
def debug_snapshot():
    """
    One endpoint to show the last run end-to-end (request -> stream -> error if any).
    """
    return {
        "time": _now(),
        "last_snapshot": LAST_SNAPSHOT or {"note": "No snapshot yet. Send a message first."},
        "last_error": LAST_ERROR or None,
        "last_stream_tail": LAST_STREAM[-30:],
    }


@agent.post("/threads/{thread_id}/runs/stream")
async def runs_stream(thread_id: str, request: Request):
    """
    Key fix: DO NOT end with status 'complete' for normal chat turns.
    AgentChat is treating that as 'close and reset UI'.

    We emit:
      metadata -> messages -> ping -> end(status='running')

    Also we ONLY emit 'messages' (no duplicate 'values').
    """
    body = await request.json()
    incoming = _normalize_messages((body.get("input") or {}).get("messages", []))

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    run_id = str(uuid.uuid4())

    async def gen():
        LAST_STREAM.clear()
        LAST_SNAPSHOT.clear()

        try:
            meta = {
                "run_id": run_id,
                "thread_id": thread_id,
                "assistant_id": body.get("assistant_id"),
                "status": "running",
            }
            _record_stream("metadata", meta)
            yield {"event": "metadata", "data": json.dumps(meta, ensure_ascii=False)}

            reply = _call_agent(thread_id)
            THREADS[thread_id].append({"role": "ai", "content": reply})

            msg_obj = {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "ai",
                "role": "ai",
                "content": reply,
            }

            _record_stream("messages", [msg_obj])
            yield {"event": "messages", "data": json.dumps([msg_obj], ensure_ascii=False)}

            ping = {"run_id": run_id, "ok": True}
            _record_stream("ping", ping)
            yield {"event": "ping", "data": json.dumps(ping, ensure_ascii=False)}

            # IMPORTANT: not 'complete'
            end = {"run_id": run_id, "status": "running"}
            _record_stream("end", end)
            yield {"event": "end", "data": json.dumps(end, ensure_ascii=False)}

            LAST_SNAPSHOT.update(
                {
                    "time": _now(),
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "request_body": body if DEBUG else {"note": "Set DEBUG=1 to include request body"},
                    "reply_preview": reply[:400],
                }
            )

        except Exception as e:
            _capture_error(thread_id=thread_id, run_id=run_id, request_body=body, e=e)

            err = {"run_id": run_id, "error": LAST_ERROR.get("error", "unknown error")}
            _record_stream("error", err)
            yield {"event": "error", "data": json.dumps(err, ensure_ascii=False)}

            end = {"run_id": run_id, "status": "failed"}
            _record_stream("end", end)
            yield {"event": "end", "data": json.dumps(end, ensure_ascii=False)}

    return EventSourceResponse(
        gen(),
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


app.include_router(agent)
