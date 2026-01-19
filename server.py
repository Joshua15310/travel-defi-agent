"""
LangGraph SDK Compliant Server
This server follows the exact LangGraph Cloud API specification.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from agent import workflow_app as graph
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    stream=sys.stderr,
    force=True
)
log = logging.getLogger(__name__)

app = FastAPI(title="Travel DeFi Agent - LangGraph SDK Compliant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# In-memory thread storage (will be replaced with checkpointer)
THREADS: Dict[str, List[Dict[str, Any]]] = {}


# Custom JSON encoder for LangChain messages
class MessageEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (AIMessage, HumanMessage, SystemMessage)):
            return {
                "type": "ai" if isinstance(obj, AIMessage) else ("human" if isinstance(obj, HumanMessage) else "system"),
                "content": obj.content,
                "id": getattr(obj, 'id', f"msg_{uuid.uuid4().hex}"),
                "additional_kwargs": getattr(obj, 'additional_kwargs', {}),
            }
        return super().default(obj)


# =============================================================================
# LangGraph SDK Standard Endpoints
# =============================================================================

@app.get("/ok")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/assistants")
async def list_assistants():
    """List available assistants (LangGraph SDK standard)"""
    return [
        {
            "assistant_id": "travel-defi-agent",
            "graph_id": "agent",
            "created_at": "2026-01-19T00:00:00Z",
            "updated_at": "2026-01-19T00:00:00Z",
            "config": {},
            "metadata": {
                "name": "Travel DeFi Agent",
                "description": "Books flights and hotels using USDC",
            }
        }
    ]


@app.get("/assistants/{assistant_id}")
async def get_assistant(assistant_id: str):
    """Get assistant details"""
    if assistant_id != "travel-defi-agent":
        raise HTTPException(status_code=404, detail="Assistant not found")
    
    return {
        "assistant_id": "travel-defi-agent",
        "graph_id": "agent",
        "created_at": "2026-01-19T00:00:00Z",
        "updated_at": "2026-01-19T00:00:00Z",
        "config": {},
        "metadata": {
            "name": "Travel DeFi Agent",
            "description": "Books flights and hotels using USDC",
        }
    }


@app.post("/threads")
async def create_thread(request: Request):
    """Create a new thread"""
    try:
        body = await request.json()
    except:
        body = {}
    
    thread_id = str(uuid.uuid4())
    THREADS[thread_id] = []
    
    log.info(f"Created thread: {thread_id}")
    
    return {
        "thread_id": thread_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "metadata": body.get("metadata", {}),
    }


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    """Get thread details"""
    if thread_id not in THREADS:
        raise HTTPException(status_code=404, detail="Thread not found")
    
    return {
        "thread_id": thread_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "metadata": {},
    }


@app.get("/threads/{thread_id}/state")
async def get_thread_state(thread_id: str):
    """Get thread state"""
    if thread_id not in THREADS:
        raise HTTPException(status_code=404, detail="Thread not found")
    
    # Get state from checkpointer
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = graph.get_state(config)
        return {
            "values": state.values if state else {},
            "next": state.next if state else [],
            "checkpoint": {
                "thread_id": thread_id,
            },
            "metadata": {},
        }
    except Exception as e:
        log.error(f"Error getting state: {e}")
        return {
            "values": {"messages": []},
            "next": [],
            "checkpoint": {"thread_id": thread_id},
            "metadata": {},
        }


@app.post("/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: Request):
    """Stream agent execution (LangGraph SDK standard)"""
    try:
        body = await request.json()
        assistant_id = body.get("assistant_id", "travel-defi-agent")
        
        # Extract input from various possible formats
        input_data = body.get("input", {})
        if not input_data:
            # Check for message in kwargs
            kwargs = body.get("kwargs", {})
            if "messages" in kwargs:
                input_data = kwargs
            elif "input" in kwargs:
                input_data = kwargs["input"]
        
        log.info(f"Stream request for thread {thread_id}: {input_data}")
        
        config = {
            "configurable": {
                "thread_id": thread_id,
            }
        }
        
        async def event_generator():
            try:
                # Use LangGraph's astream with stream_mode="values"
                async for event in graph.astream(input_data, config=config, stream_mode="values"):
                    # Send state updates
                    yield {
                        "event": "values",
                        "data": json.dumps(event, cls=MessageEncoder, ensure_ascii=False)
                    }
                
                # Send end event
                yield {"event": "end", "data": ""}
                
            except Exception as e:
                log.error(f"Stream error: {e}", exc_info=True)
                yield {
                    "event": "error",
                    "data": json.dumps({"error": str(e)})
                }
                yield {"event": "end", "data": ""}
        
        return EventSourceResponse(event_generator())
    
    except Exception as e:
        log.error(f"Endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/threads/{thread_id}/runs/wait")
async def run_wait(thread_id: str, request: Request):
    """Run and wait for completion (non-streaming)"""
    try:
        body = await request.json()
        input_data = body.get("input", {})
        
        config = {
            "configurable": {
                "thread_id": thread_id,
            }
        }
        
        log.info(f"Non-streaming run for thread {thread_id}")
        
        # Invoke the graph
        result = await graph.ainvoke(input_data, config=config)
        
        return {
            "status": "success",
            "thread_id": thread_id,
            "result": json.loads(json.dumps(result, cls=MessageEncoder)),
        }
    
    except Exception as e:
        log.error(f"Run error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/threads/{thread_id}/history")
async def get_thread_history(thread_id: str, limit: int = 100):
    """Get thread message history"""
    if thread_id not in THREADS:
        raise HTTPException(status_code=404, detail="Thread not found")
    
    try:
        # Get from checkpointer
        config = {"configurable": {"thread_id": thread_id}}
        state = graph.get_state(config)
        
        if state and "messages" in state.values:
            messages = state.values["messages"]
            return {
                "messages": json.loads(json.dumps(messages, cls=MessageEncoder))[-limit:],
                "thread_id": thread_id,
            }
        
        return {
            "messages": [],
            "thread_id": thread_id,
        }
    except Exception as e:
        log.error(f"History error: {e}")
        return {
            "messages": [],
            "thread_id": thread_id,
        }


# Root endpoint
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Travel DeFi Agent",
        "version": "1.0.0",
        "langgraph_sdk_compliant": True,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
