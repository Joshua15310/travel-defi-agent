"""
FastAPI wrapper for LangGraph agent deployment.
Adds the missing /threads/{thread_id}/history endpoint required by Warden verification.
"""

import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Import the LangGraph workflow
from agent import workflow_app, memory

# Create the FastAPI app
app_instance = FastAPI(title="Warden Travel Agent")

# Add CORS middleware
app_instance.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app_instance.get("/threads/{thread_id}/history")
async def get_thread_history(thread_id: str):
    """
    Get message history for a thread.
    This endpoint is required by Warden Hub for agent verification and Vercel integration.
    """
    try:
        # Get thread data from checkpointer
        config = {"configurable": {"thread_id": thread_id}}
        thread_state = memory.get(config)
        
        if thread_state:
            messages = thread_state.values.get("messages", [])
            return {
                "thread_id": thread_id,
                "messages": [
                    {
                        "type": getattr(msg, "type", "message"),
                        "content": getattr(msg, "content", str(msg)),
                        "role": "assistant" if getattr(msg, "type", None) == "ai" else "user"
                    }
                    for msg in (messages if isinstance(messages, list) else [])
                ]
            }
        else:
            # New thread
            return {"thread_id": thread_id, "messages": []}
    except Exception as e:
        print(f"[ERROR] get_thread_history: {e}")
        return {"thread_id": thread_id, "messages": []}


# Mount the LangGraph workflow at root
app_instance.include_router(workflow_app, prefix="")

# Export as 'app' for Uvicorn to find it
app = app_instance
