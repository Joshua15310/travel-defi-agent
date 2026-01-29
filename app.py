"""
FastAPI wrapper for LangGraph agent deployment.
Adds the missing /threads/{thread_id}/history endpoint required by Warden verification.
"""

import json
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


@app.get("/")
async def root():
    """Agent info endpoint"""
    return {
        "name": "Travel DeFi Agent",
        "description": "Book travel with DeFi integration",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.post("/threads/{thread_id}")
async def send_message(thread_id: str, request: MessageRequest):
    """
    Send a message to the agent and get a response.
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
