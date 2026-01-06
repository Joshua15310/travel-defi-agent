# 1. Use an official lightweight Python image
FROM python:3.11-slim

# 2. Set the working directory
WORKDIR /app

# 3. Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy your code
COPY . .

# 5. Expose the port
EXPOSE 8000

# 6. THE FIX: Run the Python script, NOT the uvicorn command
CMD ["python", "server.py"]