from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langserve import add_routes
# Import the agent we created in agent.py
from agent import workflow_app

app = FastAPI(title="Crypto Travel Agent")

# --- FIX: Enable CORS ---
# This tells the server: "Accept requests from any website or local file"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows ALL origins (crucial for local HTML files)
    allow_credentials=True,
    allow_methods=["*"],  # Allows all HTTP methods (POST, GET, etc.)
    allow_headers=["*"],  # Allows all headers
)

# Add the agent routes
add_routes(app, workflow_app, path="/agent")

if __name__ == "__main__":
    import uvicorn
    # Look for the PORT environment variable (Render sets this automatically)
    import os
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)