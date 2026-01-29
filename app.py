"""
FastAPI wrapper for LangGraph agent deployment.
Supports Vercel AgentChat and Warden Hub integration.
"""

import json
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Import the LangGraph workflow
from agent import workflow_app, memory

# Create the FastAPI app
app = FastAPI(title="Warden Travel Agent")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request models
class MessageRequest(BaseModel):
    message: str


class ThreadCreateRequest(BaseModel):
    """Request format for creating/initializing a thread"""
    metadata: Optional[dict] = None
    limit: Optional[int] = 100
    offset: Optional[int] = 0


class SearchRequest(BaseModel):
    """Request format for searching/sending messages"""
    message: Optional[str] = None
    metadata: Optional[dict] = None
    limit: Optional[int] = 100
    offset: Optional[int] = 0


@app.get("/")
async def root():
    """Root endpoint - agent info"""
    return {
        "name": "Travel DeFi Agent",
        "description": "Book travel with DeFi integration",
        "version": "1.0.0"
    }


@app.get("/info")
async def info():
    """Agent info endpoint (Vercel compatibility)"""
    return {
        "name": "Travel DeFi Agent",
        "description": "Book travel with DeFi integration",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.post("/threads")
async def create_thread(request: Optional[ThreadCreateRequest] = None):
    """
    Create/initialize a new thread.
    Required by Vercel AgentChat app.
    """
    try:
        # Generate a simple thread ID
        import uuid
        thread_id = str(uuid.uuid4())
        
        # Initialize thread state in memory
        config = {"configurable": {"thread_id": thread_id}}
        
        # Return thread info
        return {
            "thread_id": thread_id,
            "messages": []
        }
    except Exception as e:
        print(f"[ERROR] create_thread: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/threads/{thread_id}/search")
async def search_thread(thread_id: str, request: SearchRequest):
    """
    Search/send a message to the agent.
    Format compatible with Vercel AgentChat app.
    """
    try:
        # Get message from request
        message = request.message if request.message else "Help me with travel"
        
        # Invoke the workflow with the thread_id
        config = {"configurable": {"thread_id": thread_id}}
        
        input_data = {"messages": [{"role": "user", "content": message}]}
        
        # Run the workflow
        result = workflow_app.invoke(input_data, config=config)
        
        # Return the final state with messages
        messages = result.get("messages", [])
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
    except Exception as e:
        print(f"[ERROR] search_thread: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/threads/{thread_id}")
async def send_message(thread_id: str, request: MessageRequest):
    """
    Send a message to the agent and get a response.
    Direct message endpoint (alternative to /search).
    """
    try:
        # Invoke the workflow with the thread_id
        config = {"configurable": {"thread_id": thread_id}}
        
        input_data = {"messages": [{"role": "user", "content": request.message}]}
        
        # Run the workflow
        result = workflow_app.invoke(input_data, config=config)
        
        # Return the final state with messages
        messages = result.get("messages", [])
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
    except Exception as e:
        print(f"[ERROR] send_message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/threads/{thread_id}/history")
@app.post("/threads/{thread_id}/history")
async def get_thread_history(thread_id: str):
    """
    Get message history for a thread.
    Required by Warden Hub and Vercel integration.
    Supports both GET and POST methods.
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
