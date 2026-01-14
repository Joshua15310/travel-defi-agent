FROM python:3.11-slim

# 1. Install system dependencies (including build tools for Web3)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Set working directory
WORKDIR /app

# 3. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy your agent code and configuration
COPY . .

# 5. Expose the port
ENV PORT=8000
EXPOSE 8000

# 6. CRITICAL CHANGE: Use the official LangGraph CLI to serve
# This automatically creates the /agent/assistants/search endpoint
CMD exec langgraph up --host 0.0.0.0 --port $PORT