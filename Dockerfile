# 1. Use an official lightweight Python image
FROM python:3.11-slim

# 2. Set the working directory
WORKDIR /app

# 3. Set Python to unbuffered mode for real-time logging
ENV PYTHONUNBUFFERED=1

# 4. Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy your code
COPY . .

# 6. Expose the port
EXPOSE 8000

# 7. Run with uvicorn (Render will handle this with their CMD override)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]