# =============================================================================
# Stage 1: Build Frontend Dashboard (React + Vite)
# =============================================================================
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci || npm install

COPY frontend/ ./
RUN npm run build

# =============================================================================
# Stage 2: Python Runtime & Production Server
# =============================================================================
FROM python:3.11-slim
WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python backend dependencies
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r backend/requirements.txt

# Pre-download NLTK sentence-splitter data for speech VAD
RUN python -m nltk.downloader punkt_tab

# Copy backend codebase and launcher
COPY backend/ ./backend/
COPY start.py ./

# Copy compiled frontend from Stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Expose port
EXPOSE 8000

ENV PORT=8000 \
    PYTHONUNBUFFERED=1

# Run the launcher with --no-tunnel on production server
CMD ["python", "start.py", "--no-tunnel", "--host", "0.0.0.0", "--port", "8000"]
