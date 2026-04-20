# Use Node 18 slim as base
FROM node:18-slim

# Install system dependencies for Python and GDAL
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    gdal-bin \
    libgdal-dev \
    libproj-dev \
    proj-bin \
    libgl1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create and activate virtual environment for Python dependencies
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir -r requirements.txt

# Install Node.js dependencies
COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

# Copy the rest of the application
COPY . .

# Set environment variables
ENV NODE_ENV=production
ENV PORT=9008
ENV DATA_DIR=/app/data
# Optional overrides for public URLs and large login/images responses
ENV PUBLIC_BASE_URL=
ENV PUBLIC_IP=
ENV MAX_LOGIN_IMAGES_JSON_BYTES=209715200

# Create data directory with proper permissions
RUN mkdir -p data/users && chmod -R 777 data

EXPOSE 9008

# Start the application
CMD ["node", "server.js"]