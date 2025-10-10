# ---------------------------
#  GATEWAY SERVICE DOCKERFILE
# ---------------------------

# Use lightweight official Python image
FROM python:3.11-slim

# Set working directory inside container
WORKDIR /app

# Copy the full application first (so code changes always trigger rebuild)
COPY . .

# Install dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Expose FastAPI / Uvicorn port
EXPOSE 8000

# Start the FastAPI gateway
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000"]
