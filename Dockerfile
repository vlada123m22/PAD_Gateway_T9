FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install minimal build tools (Debian/Ubuntu)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       git \
    && rm -rf /var/lib/apt/lists/*

# Prefer using prebuilt wheels available on manylinux/musllinux
ENV PIP_NO_BUILD_ISOLATION=0
ENV PIP_PREFER_BINARY=1

# Copy requirements and install Python dependencies
COPY requirements.txt .
# Upgrade basic build tooling and install dependencies
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose port
EXPOSE 8000

# Start the app
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000"]
