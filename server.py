from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRouter
from httpx import AsyncClient

import agent as agent_module
from agent import AIMessage, HumanMessage, SystemMessage

# Configure logging to stderr (always unbuffered)
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    stream=sys.stderr,
    force=True
)
log = logging.getLogger(__name__)


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
    max_age=600,
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

    # Ensure content is a clean string
    if not isinstance(content, str):
        content = str(content)
    
    # Remove any leading/trailing whitespace
    content = (content or "").strip()

    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": msg_type,
        "role": final_role,  # AgentChat expects: user, assistant, or system
        "content": content,
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


def _sanitize_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure all messages in history have clean, properly formatted content with all required SDK fields"""
    sanitized = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        
        # Extract and normalize role
        role = str(msg.get("role", "user")).lower()
        if role not in ("user", "assistant"):
            role = "user"
        
        # Extract and normalize type
        msg_type = str(msg.get("type", "human")).lower()
        if msg_type not in ("human", "ai"):
            msg_type = "human" if role == "user" else "ai"
        
        # Get or create ID
        msg_id = msg.get("id")
        if not msg_id:
            msg_id = f"msg_{uuid.uuid4().hex}"
        
        # Extract content - handle all possible formats
        content = msg.get("content", "")
        if isinstance(content, str):
            # If content is a stringified list, try to extract text
            if content.startswith("[{") and "type" in content and "text" in content:
                try:
                    parsed = ast.literal_eval(content)
                    text_parts = []
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_parts.append(str(item.get("text", "")))
                    if text_parts:
                        content = "\n".join(text_parts)
                except:
                    pass
        elif isinstance(content, list):
            # Handle list of content blocks
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    text_parts.append(item)
            content = "\n".join(text_parts) if text_parts else str(content)
        else:
            content = str(content) if content else ""
        
        # Create properly formatted message for SDK
        clean_msg = {
            "id": str(msg_id),
            "type": msg_type,
            "role": role,
            "content": content.strip() if isinstance(content, str) else str(content)
        }
        
        sanitized.append(clean_msg)
    
    return sanitized


def _normalize_incoming_messages(messages: Any) -> List[Dict[str, Any]]:
    """
    AgentChat sends input.messages in different formats:
    1. Simple: [{role: "user", content: "hello"}, ...]
    2. Complex: [{role: "user", content: [{"type": "text", "text": "hello"}]}, ...]
    
    We normalize to extract the actual text content.
    """
    if not isinstance(messages, list):
        return []
    out: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        content_raw = m.get("content", "")
        
        # Extract actual text content
        if isinstance(content_raw, list):
            # Format: [{"type": "text", "text": "hello"}, ...]
            text_parts = []
            for item in content_raw:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
            content = " ".join(text_parts) if text_parts else str(content_raw)
        elif isinstance(content_raw, str):
            # Check if it's a stringified list (edge case from buggy frontend)
            if content_raw.startswith("[{") and "type" in content_raw and "text" in content_raw:
                try:
                    # Try to parse it as Python literal
                    parsed = ast.literal_eval(content_raw)
                    text_parts = []
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                    content = " ".join(text_parts) if text_parts else content_raw
                except:
                    content = content_raw
            else:
                content = content_raw
        else:
            content = str(content_raw)
        
        out.append(_new_msg(role=role, content=content))
    return out


def _to_langchain_messages(history: List[Dict[str, Any]]):
    lc = []
    for m in history:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            lc.append(SystemMessage(content=content))
        elif role in ("ai", "assistant"):  # Support both "ai" and "assistant"
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
                text = str(last.content)
                log.info(f"_extract_ai_text: extracted from message object: {text[:100]}")
                return text
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                text = last["content"]
                log.info(f"_extract_ai_text: extracted from dict: {text[:100]}")
                return text
    log.warning(f"_extract_ai_text: Using fallback response, result type: {type(result)}, result: {str(result)[:200]}")
    return "I'm here—tell me what you want to book."


def _call_agent(thread_id: str) -> str:
    if not hasattr(agent_module, "app"):
        raise RuntimeError(
            "agent.py must export:\n"
            "app = workflow_app\n"
            "graph = workflow_app"
        )

    lc_history = _to_langchain_messages(THREADS.get(thread_id, []))
    log.info(f"_call_agent: calling agent with {len(lc_history)} messages")
    result = agent_module.app.invoke(
        {"messages": lc_history},
        config={"configurable": {"thread_id": thread_id}},
    )
    log.info(f"_call_agent: raw result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
    text = _extract_ai_text(result)
    log.info(f"_call_agent: final extracted text: {text[:100]}")
    return text


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


@app.options("/{path:path}")
def options_handler():
    return JSONResponse(status_code=200)


@app.get("/status")
def status():
    return {"status": "ok"}


@app.get("/assistants/search")
def assistants_search():
    """LangGraph SDK Standard: List available assistants"""
    return _assistant_catalog()


@app.get("/agent/assistants/search")
def assistants_search_agent():
    """LangGraph SDK Standard: List available assistants (with /agent prefix)"""
    return _assistant_catalog()


@app.get("/info")
def info():
    """LangGraph SDK Standard: Get agent info"""
    return _info_payload()


@app.get("/agent/info")
def info_agent():
    """LangGraph SDK Standard: Get agent info (with /agent prefix)"""
    return _info_payload()


@app.post("/threads")
def create_thread():
    """LangGraph SDK Standard: Create new thread"""
    tid = str(uuid.uuid4())
    THREADS[tid] = []
    return {"thread_id": tid}


@app.post("/agent/threads")
def create_thread_agent():
    """LangGraph SDK Standard: Create new thread (with /agent prefix)"""
    tid = str(uuid.uuid4())
    THREADS[tid] = []
    return {"thread_id": tid}


@app.post("/chat")
async def simple_chat(request: Request):
    """Simple chat endpoint for HTML frontend (non-streaming)"""
    try:
        body = await request.json()
        message = body.get("message", "").strip()
        thread_id = body.get("thread_id", str(uuid.uuid4()))
        
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)
        
        # Initialize thread if needed
        if thread_id not in THREADS:
            THREADS[thread_id] = []
        
        # Add user message
        user_msg = _new_msg("user", message)
        THREADS[thread_id].append(user_msg)
        log.info(f"Chat: thread {thread_id} received message: {message[:50]}")
        
        # Get agent response
        reply = _call_agent(thread_id)
        ai_msg = _new_msg("assistant", reply)
        THREADS[thread_id].append(ai_msg)
        
        log.info(f"Chat: thread {thread_id} agent replied: {reply[:50]}")
        return {"reply": reply, "thread_id": thread_id}
        
    except Exception as e:
        log.error(f"Chat error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/threads/search")
def threads_search():
    """LangGraph SDK Standard: Search/list threads"""
    return [{"thread_id": t} for t in THREADS.keys()]


@app.post("/agent/threads/search")
def threads_search_agent():
    """LangGraph SDK Standard: Search/list threads (with /agent prefix)"""
    return [{"thread_id": t} for t in THREADS.keys()]


# IMPORTANT: support BOTH GET and POST (different AgentChat builds use different methods)
@app.get("/threads/{thread_id}/history")
def thread_history_get(thread_id: str):
    """LangGraph SDK Standard: Get thread message history"""
    history = THREADS.get(thread_id, [])
    # Ensure all messages have clean content
    result = _sanitize_history(history)
    msg_summary = [f"{m.get('role')}:{m.get('content')[:30]}" for m in result]
    log.info(f"GET /threads/{thread_id}/history returning {len(result)} messages: {msg_summary}")
    return result


@app.get("/agent/threads/{thread_id}/history")
def thread_history_get_agent(thread_id: str):
    """LangGraph SDK Standard: Get thread message history (with /agent prefix)"""
    history = THREADS.get(thread_id, [])
    result = _sanitize_history(history)
    msg_summary = [f"{m.get('role')}:{m.get('content')[:30]}" for m in result]
    log.info(f"GET /agent/threads/{thread_id}/history returning {len(result)} messages: {msg_summary}")
    return result


@app.post("/threads/{thread_id}/history")
def thread_history_post(thread_id: str):
    """LangGraph SDK Standard: Get thread message history"""
    history = THREADS.get(thread_id, [])
    # Ensure all messages have clean content
    result = _sanitize_history(history)
    msg_summary = [f"{m.get('role')}:{m.get('content')[:30]}" for m in result]
    log.info(f"POST /threads/{thread_id}/history returning {len(result)} messages: {msg_summary}")
    return result


@app.get("/debug/last_error")
def debug_last_error():
    """Debug: Get last error"""
    return LAST_ERROR or {"ok": True}


@app.get("/debug/last_stream")
def debug_last_stream():
    """Debug: Get last stream events"""
    return {"count": len(LAST_STREAM), "last": LAST_STREAM[-80:]}


@app.get("/debug/threads")
def debug_threads():
    """Debug: Get all thread data"""
    return {
        "threads": {
            tid: {
                "message_count": len(msgs),
                "messages": msgs
            } for tid, msgs in THREADS.items()
        }
    }


@app.post("/threads/{thread_id}/runs/stream")
async def runs_stream(thread_id: str, request: Request):
    """LangGraph SDK Standard: Stream agent execution for a thread"""
    body = await request.json()
    incoming = _normalize_incoming_messages((body.get("input") or {}).get("messages", []))

    log.info(f"/threads/{thread_id}/runs/stream - Received {len(incoming)} incoming messages")
    for msg in incoming:
        log.info(f"  - {msg.get('type')}/{msg.get('role')}: {msg.get('content')[:50]}...")

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    log.info(f"Thread {thread_id} now has {len(THREADS[thread_id])} messages before agent call")

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
            log.info(f"YIELDING metadata event to client")
            yield f"event: metadata\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

            # 2. Call agent
            reply = _call_agent(thread_id)
            log.info(f"Agent reply: {reply[:100]}...")
            ai_msg = _new_msg("assistant", reply)  # Use "assistant" instead of "ai"
            log.info(f"Created AI message: type={ai_msg.get('type')}, role={ai_msg.get('role')}, content={ai_msg.get('content')[:50]}...")
            THREADS[thread_id].append(ai_msg)
            log.info(f"Thread {thread_id} now has {len(THREADS[thread_id])} messages after agent response")

            # 3. Get full conversation history
            full_history = _sanitize_history(THREADS.get(thread_id, []))
            log.info(f"Full thread history: {len(full_history)} messages")

            # 4. Send values event with complete conversation state
            # This is the PRIMARY state update that SDK reads
            values_payload = {
                "messages": full_history
            }
            _record("values", values_payload)
            values_json = json.dumps(values_payload, ensure_ascii=False)
            log.info(f"YIELDING values event with {len(full_history)} messages")
            log.info(f"VALUES EVENT PAYLOAD: {values_json}")
            yield f"event: values\ndata: {values_json}\n\n"
            await asyncio.sleep(0.1)

            # 5. Send end event marking successful completion
            end = {
                "run_id": run_id,
                "thread_id": thread_id
            }
            _record("end", end)
            log.info(f"YIELDING end event")
            yield f"event: end\ndata: {json.dumps(end, ensure_ascii=False)}\n\n"

        except Exception as e:
            _capture_error(thread_id, run_id, body, e)
            err = {
                "run_id": run_id,
                "error": LAST_ERROR.get("error", "unknown error"),
                "status": "error"
            }
            _record("error", err)
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"

            end = {
                "run_id": run_id,
                "status": "error",
                "thread_id": thread_id
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
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )


# =============================================================================
# AGENT ROUTER - Mirror endpoints for Vercel app compatibility
# Keeps /agent/* paths for existing Vercel app while supporting root paths
# for CTO's LangGraph SDK integration
# =============================================================================

agent = APIRouter(prefix="/agent")


@agent.get("/assistants/search")
def agent_assistants_search():
    """Vercel compatibility: List available assistants"""
    return _assistant_catalog()


@agent.get("/info")
def agent_info():
    """Vercel compatibility: Get agent info"""
    return _info_payload()


@agent.post("/threads")
def agent_create_thread():
    """Vercel compatibility: Create new thread"""
    tid = str(uuid.uuid4())
    THREADS[tid] = []
    return {"thread_id": tid}


@agent.post("/threads/search")
def agent_threads_search():
    """Vercel compatibility: Search/list threads"""
    return [{"thread_id": t} for t in THREADS.keys()]


@agent.get("/threads/{thread_id}/history")
def agent_thread_history_get(thread_id: str):
    """Vercel compatibility: Get thread message history"""
    history = THREADS.get(thread_id, [])
    result = _sanitize_history(history)
    msg_summary = [f"{m.get('role')}:{m.get('content')[:30]}" for m in result]
    log.info(f"GET /agent/threads/{thread_id}/history returning {len(result)} messages: {msg_summary}")
    return result


@agent.post("/threads/{thread_id}/history")
def agent_thread_history_post(thread_id: str):
    """Vercel compatibility: Get thread message history"""
    history = THREADS.get(thread_id, [])
    result = _sanitize_history(history)
    msg_summary = [f"{m.get('role')}:{m.get('content')[:30]}" for m in result]
    log.info(f"POST /agent/threads/{thread_id}/history returning {len(result)} messages: {msg_summary}")
    return result


@agent.post("/threads/{thread_id}/runs/stream")
async def agent_runs_stream(thread_id: str, request: Request):
    """Vercel compatibility: Stream agent execution for a thread"""
    body = await request.json()
    incoming = _normalize_incoming_messages((body.get("input") or {}).get("messages", []))

    log.info(f"/agent/threads/{thread_id}/runs/stream - Received {len(incoming)} incoming messages")
    for msg in incoming:
        log.info(f"  - {msg.get('type')}/{msg.get('role')}: {msg.get('content')[:50]}...")

    THREADS.setdefault(thread_id, [])
    if incoming:
        THREADS[thread_id].extend(incoming)

    log.info(f"Thread {thread_id} now has {len(THREADS[thread_id])} messages before agent call")

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
            log.info(f"YIELDING metadata event to client")
            yield f"event: metadata\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

            # 2. Call agent
            reply = _call_agent(thread_id)
            log.info(f"Agent reply: {reply[:100]}...")
            ai_msg = _new_msg("assistant", reply)
            log.info(f"Created AI message: type={ai_msg.get('type')}, role={ai_msg.get('role')}, content={ai_msg.get('content')[:50]}...")
            THREADS[thread_id].append(ai_msg)
            log.info(f"Thread {thread_id} now has {len(THREADS[thread_id])} messages after agent response")

            # 3. Get full conversation history
            full_history = _sanitize_history(THREADS.get(thread_id, []))
            log.info(f"Full thread history: {len(full_history)} messages")

            # 4. Send values event with complete conversation state
            # This is the PRIMARY state update that SDK reads
            values_payload = {
                "messages": full_history
            }
            _record("values", values_payload)
            values_json = json.dumps(values_payload, ensure_ascii=False)
            log.info(f"YIELDING values event with {len(full_history)} messages")
            log.info(f"VALUES EVENT PAYLOAD: {values_json}")
            yield f"event: values\ndata: {values_json}\n\n"
            await asyncio.sleep(0.1)

            # 5. Send end event marking successful completion
            end = {
                "run_id": run_id,
                "thread_id": thread_id
            }
            _record("end", end)
            log.info(f"YIELDING end event")
            yield f"event: end\ndata: {json.dumps(end, ensure_ascii=False)}\n\n"
            
            # Keep stream alive briefly to ensure all events delivered
            await asyncio.sleep(0.2)
            log.info(f"SSE stream finished for thread {thread_id}")

        except Exception as e:
            _capture_error(thread_id, run_id, body, e)
            err = {
                "run_id": run_id,
                "error": LAST_ERROR.get("error", "unknown error"),
                "status": "error"
            }
            _record("error", err)
            log.info(f"YIELDING error event")
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"

            end = {
                "run_id": run_id,
                "status": "error",
                "thread_id": thread_id
            }
            _record("end", end)
            log.info(f"YIELDING end event - stream error")
            yield f"event: end\ndata: {json.dumps(end, ensure_ascii=False)}\n\n"

    # Use StreamingResponse with proper SSE headers
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )


# Debug endpoints for agent router (so they're accessible at /agent/debug/*)
@agent.get("/debug/last_error")
def agent_debug_last_error():
    """Debug: Get last error"""
    return LAST_ERROR or {"ok": True}


@agent.get("/debug/last_stream")
def agent_debug_last_stream():
    """Debug: Get last stream events"""
    return {"count": len(LAST_STREAM), "last": LAST_STREAM[-80:]}


@agent.get("/debug/threads")
def agent_debug_threads():
    """Debug: Get all thread data"""
    return {
        "threads": {
            tid: {
                "message_count": len(msgs),
                "messages": msgs
            } for tid, msgs in THREADS.items()
        }
    }


app.include_router(agent)