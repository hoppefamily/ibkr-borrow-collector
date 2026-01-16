FROM python:3.11-alpine

# Install system dependencies
RUN apk add --no-cache \
    xdelta3 \
    ca-certificates \
    && rm -rf /var/cache/apk/*

# Create non-root user
RUN adduser -D -u 1000 collector

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy collector script
COPY collector.py .
RUN chmod +x collector.py

# Switch to non-root user
USER collector

# Create cache directory
RUN mkdir -p /home/collector/.ibkr-baselines

# Default command
ENTRYPOINT ["python", "/app/collector.py"]
CMD ["--help"]
