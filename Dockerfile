# Use lightweight Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements (faster builds if dependencies don’t change)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose the gateway port
EXPOSE 8000

# Start the FastAPI app with uvicorn
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000"]
