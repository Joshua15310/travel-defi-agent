FROM python:3.11-slim

# 1. Install system dependencies required for the build
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    libssl-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

RUN pip install --upgrade pip

# 5. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy your agent code
COPY . .

# 7. Expose the port
ENV PORT=8000
EXPOSE 8000

CMD exec langgraph dev --host 0.0.0.0 --port $PORT