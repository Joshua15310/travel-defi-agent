from __future__ import annotations

import inspect
import json
import uuid
from typing import Any, Dict, List, Optional, Callable, Tuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from sse_starlette.sse import EventSourceResponse

import agent as agent_module


# -----------------------------------------------------------------------------
# App + CORS
# -----------------------------------------------------------------------------

app = FastAPI()

# IMPORTANT:
# Don't use "*" with allow_credentials=True in production.
# For now, we include agentchat + "*" so you can move fast.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://agentchat.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "*",
    ],
    allow_credentials=False,  # keep False to avoid wildcard+credentials issues
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# -----------------------------------------------------------------------------
# In-memory thread store (resets on redeploy)
# -----------------------------------------------------------------------------

THREADS: Dict[str, List[Dict[str, str]]] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

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


def _extract_text(result: Any) -> str:
    # plain string
    if isinstance(result, str) and result.strip():
        return result.strip()

    # dict shapes
    if isinstance(result, dict):
        # {result: {reply: "..."}}
        if isinstance(result.get("result"), dict) and isinstance(result["result"].get("reply"), str):
            return result["result"]["reply"]

        # {reply: "..."}
        if isinstance(result.get("reply"), str):
            return result["reply"]

        # {messages: [...]} (langgraph style)
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"]

    # list shapes
    if isinstance(result, list) and result:
        last = result[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), str):
            return last["content"]

    return ""


def _callable_name(obj: Any) -> str:
    try:
        return getattr(obj, "__name__", obj.__class__.__name__)
    except Exception:
        return str(type(obj))


def find_agent_entrypoint() -> Tuple[Optional[Callable[..., Any]], str]:
    """
    Try to find how to call agent.py in a robust way.
    Returns: (callable_or_none, description_string)

    Supported patterns:
    - agent_module.app.invoke({"messages": [...]})
    - agent_module.graph.invoke({"messages": [...]})
    - agent_module.<fn>(...) where fn in common names
    - agent_module.AGENT / agent_module.APP / agent_module.GRAPH as objects with invoke()
    - any callable in agent_module that looks like it accepts (messages) or (input/messages dict)
    """
    # 1) Common object names with invoke
    for attr in ("app", "graph", "APP", "GRAPH", "AGENT"):
        obj = getattr(agent_module, attr, None)
        if obj is None:
            continue
        invoke = getattr(obj, "invoke", None)
        if callable(invoke):
            return (lambda history, _invoke=invoke: _invoke({"messages": history})), f"{attr}.invoke"

    # 2) Common function names
    for fn_name in ("run", "invoke", "chat", "respond", "reply", "main"):
        fn = getattr(agent_module, fn_name, None)
        if callable(fn):
            return fn, f"function:{fn_name}"

    # 3) Last resort: scan module for any callable that isn't private and isn't an imported class
    candidates: List[Tuple[str, Callable[..., Any]]] = []
    for name, value in vars(agent_module).items():
        if name.startswith("_"):
            continue
        if callable(value):
            candidates.append((name, value))

    # prefer short names that look intentional
    preferred_order = ["agent", "pipeline", "assistant", "handler", "serve", "run_agent"]
    for pref in preferred_order:
        for name, fn in candidates:
            if name == pref:
                return fn, f"function:{name}"

    # if there is exactly one reasonable callable, take it
    # (exclude builtins/types that can appear as callables)
    filtered: List[Tuple[str, Callable[..., Any]]] = []
    for name, fn in candidates:
        if inspect.isfunction(fn) or inspect.ismethod(fn):
            filtered.append((name, fn))

    if len(filtered) == 1:
        name, fn = filtered[0]
        return fn, f"function:{name}"

    return None, "none"


def run_agent_with_history(history: List[Dict[str, str]]) -> str:
    fn, desc = find_agent_entrypoint()
    if fn is None:
        raise RuntimeError(
            "Could not find a compatible entrypoint in agent.py. "
            "Use /debug/agent to see what server detects, then we will wire it precisely."
        )

    # Try calling styles in a safe sequence:
    # A) fn(history)
    # B) fn({"messages": history})
    # C) fn(messages=history)
    # D) fn(input={"messages": history})
    call_errors: List[str] = []

    for style in ("history", "dict_messages", "kw_messages", "kw_input"):
        try:
            if style == "history":
                out = fn(history)
            elif style == "dict_messages":
                out = fn({"messages": history})
            elif style == "kw_messages":
                out = fn(messages=history)  # type: ignore
            else:
                out = fn(input={"messages": history})  # type: ignore

            text = _extract_text(out)
            if text:
                return text
        except Exception as e:
            call_errors.append(f"{desc}:{style}:{type(e).__name__}:{str(e)[:160]}")

    # If it returned but we couldn't extract text, fail with context
    raise RuntimeError("Agent callable executed but no reply extracted. Errors: " + " | ".join(call_errors[:4]))


# -----------------------------------------------------------------------------
# Debug endpoint (so we stop guessing)
# -----------------------------------------------------------------------------

@app.get("/debug/agent")
def debug_agent():
    fn, desc = find_agent_entrypoint()
    exported = sorted([k for k in vars(agent_module).keys() if not k.startswith("_")])
    return {
        "detected_entrypoint": desc,
        "detected_callable": _callable_name(fn) if fn else None,
        "exports_sample": exported[:60],
        "has_app": hasattr(agent_module, "app"),
        "has_graph": hasattr(agent_module, "graph"),
        "has_run": hasattr(agent_module, "run"),
        "has_invoke": hasattr(agent_module, "invoke"),
        "has_chat": hasattr(agent_module, "chat"),
    }


# -----------------------------------------------------------------------------
# Root endpoints (CTO checks)
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
    reply = run_agent_with_history(messages)
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


@agent.post("/run")
async def agent_run(request: Request):
    return await run(request)


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
async def agent_runs_stream(thread_id: str, request: Request):
    """
    Must NEVER throw (AgentChat will treat it as CORS/failed fetch).
    If agent fails, stream a clean error message as AI content.
    """
    body = await request.json()
    incoming = _normalize_messages((body.get("input") or {}).get("messages", []))

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    async def gen():
        try:
            reply_text = run_agent_with_history(THREADS[thread_id])
            THREADS[thread_id].append({"role": "ai", "content": reply_text})

            yield {
                "event": "values",
                "data": json.dumps(
                    {"messages": [{"role": "ai", "content": reply_text}], "requirements_complete": False},
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "end",
                "data": json.dumps({"status": "complete"}, ensure_ascii=False),
            }

        except Exception as e:
            # Stream a readable error back to UI instead of crashing
            msg = f"Server error while running agent: {type(e).__name__}: {str(e)}"
            THREADS[thread_id].append({"role": "ai", "content": msg})

            yield {
                "event": "values",
                "data": json.dumps(
                    {"messages": [{"role": "ai", "content": msg}], "requirements_complete": True},
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "end",
                "data": json.dumps({"status": "complete"}, ensure_ascii=False),
            }

    return EventSourceResponse(gen())


app.include_router(agent)
