FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install dependencies including FastAPI and uvicorn
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn[standard]

# Copy the application
COPY . .

# Expose port
EXPOSE 8000

# Run the FastAPI wrapper that includes missing /threads/{thread_id}/history endpoint
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]