FROM python:3.11-slim

# 1. Install system dependencies
# CRITICAL FIX: We add 'cargo' and 'pkg-config' to compile the Rust dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    cargo \
    pkg-config \
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

# 6. Run the official LangGraph CLI
# This automatically serves the endpoints the CTO is looking for
CMD exec langgraph dev --host 0.0.0.0 --port $PORT