from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from sse_starlette.sse import EventSourceResponse

import agent as agent_module  # IMPORTANT: import the module, not a missing symbol


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
        "*",  # ok for demo; restrict later if needed
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# In-memory storage (good enough for demo; resets on redeploy)
# -----------------------------------------------------------------------------

# thread_id -> list[message]
# message shape we store: {"role": "user"|"ai"|"system", "content": "..."}
THREADS: Dict[str, List[Dict[str, str]]] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _normalize_messages(messages: Any) -> List[Dict[str, str]]:
    """
    Ensure we always work with a list of {"role": str, "content": str}.
    """
    if not isinstance(messages, list):
        return []
    cleaned: List[Dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "")).strip() or "user"
        content = str(m.get("content", "")).strip()
        cleaned.append({"role": role, "content": content})
    return cleaned


def _extract_text_from_agent_result(result: Any) -> str:
    """
    Try hard to pull an assistant reply string from whatever agent.py returns.
    This keeps agent.py untouched while supporting multiple possible styles.
    """
    # If agent returns a plain string
    if isinstance(result, str):
        return result

    # If agent returns {"result": {"reply": "..."}}
    if isinstance(result, dict):
        if isinstance(result.get("result"), dict) and isinstance(result["result"].get("reply"), str):
            return result["result"]["reply"]

        # If agent returns {"reply": "..."}
        if isinstance(result.get("reply"), str):
            return result["reply"]

        # If agent returns {"messages": [...]} (LangGraph style)
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]

    # If agent returns a list of messages
    if isinstance(result, list) and result:
        last = result[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), str):
            return last["content"]

    # Fallback
    return "I’m here—tell me what you want to book and your budget."


def run_agent_with_history(history: List[Dict[str, str]]) -> str:
    """
    Adapter that calls your existing agent.py in a compatible way.
    We attempt common entrypoints without modifying agent.py.
    """
    # 1) LangGraph app/graph objects (most common)
    for attr in ("app", "graph"):
        obj = getattr(agent_module, attr, None)
        if obj is not None:
            # Try invoke({"messages": history})
            invoke = getattr(obj, "invoke", None)
            if callable(invoke):
                try:
                    out = invoke({"messages": history})
                    return _extract_text_from_agent_result(out)
                except Exception:
                    pass

            # Try stream(...) not needed here (we do SSE ourselves)

    # 2) A function named run(messages) or invoke(messages)
    for fn_name in ("run", "invoke", "chat"):
        fn = getattr(agent_module, fn_name, None)
        if callable(fn):
            try:
                out = fn(history)
                return _extract_text_from_agent_result(out)
            except Exception:
                pass

    # 3) Last resort: if agent_module has a callable 'main' or 'agent'
    for fn_name in ("agent", "main"):
        fn = getattr(agent_module, fn_name, None)
        if callable(fn):
            try:
                out = fn(history)
                return _extract_text_from_agent_result(out)
            except Exception:
                pass

    # If nothing matched, surface a clear error (better than silent wrong behavior)
    raise RuntimeError(
        "Could not find a compatible entrypoint in agent.py. "
        "Expected agent_module.app/graph with .invoke, or a run()/invoke()/chat() function."
    )


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


# -----------------------------------------------------------------------------
# Root endpoints (CTO checks)
# -----------------------------------------------------------------------------

@app.get("/")
def root():
    # avoids Render healthcheck 404 noise
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

    reply = run_agent_with_history(messages)
    return {"result": {"reply": reply, "status": "success"}}


# -----------------------------------------------------------------------------
# /agent endpoints (AgentChat / LangGraph UI expectations)
# -----------------------------------------------------------------------------

agent = APIRouter(prefix="/agent")


@agent.get("/info")
def agent_info():
    return _info_payload()


@agent.get("/assistants/search")
def agent_assistants_search():
    return _assistant_catalog()


@agent.post("/run")
async def agent_run(request: Request):
    # Some UIs hit /agent/run
    return await run(request)


@agent.post("/threads")
def agent_create_thread():
    thread_id = str(uuid.uuid4())
    THREADS[thread_id] = []
    return {"thread_id": thread_id}


@agent.post("/threads/search")
def agent_threads_search():
    # IMPORTANT: return an array (AgentChat uses .forEach)
    return [{"thread_id": tid} for tid in THREADS.keys()]


@agent.post("/threads/{thread_id}/history")
def agent_thread_history(thread_id: str):
    # IMPORTANT: return an array of messages (AgentChat uses .forEach)
    return THREADS.get(thread_id, [])


@agent.post("/threads/{thread_id}/runs/stream")
async def agent_runs_stream(thread_id: str, request: Request):
    """
    AgentChat expects SSE. It will parse JSON from the `data:` field.
    We must NEVER emit empty data or invalid JSON.
    """
    body = await request.json()

    # AgentChat typically sends: {"input": {"messages": [...]}}
    incoming = _normalize_messages((body.get("input") or {}).get("messages", []))

    # Ensure thread exists
    THREADS.setdefault(thread_id, [])

    # Append incoming user messages to history
    if incoming:
        THREADS[thread_id].extend(incoming)

    # Generate AI reply using full history (this is where your Groks vibe comes back)
    reply_text = run_agent_with_history(THREADS[thread_id])

    # Append AI message to stored history
    THREADS[thread_id].append({"role": "ai", "content": reply_text})

    async def event_gen():
        # values event (what AgentChat listens to)
        payload = {
            "messages": [{"role": "ai", "content": reply_text}],
            "requirements_complete": False,
        }
        yield {"event": "values", "data": json.dumps(payload, ensure_ascii=False)}

        # end event with JSON (avoid "Unexpected end of JSON input")
        yield {"event": "end", "data": json.dumps({"status": "complete"}, ensure_ascii=False)}

    return EventSourceResponse(event_gen())


app.include_router(agent)
