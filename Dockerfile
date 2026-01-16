FROM python:3.11-alpine

# Install system dependencies and create non-root user in one layer
RUN apk add --no-cache xdelta3 ca-certificates \
    && rm -rf /var/cache/apk/* \
    && adduser -D -u 1000 collector \
    && mkdir -p /home/collector/.ibkr-baselines \
    && chown -R collector:collector /home/collector

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies (separate layer for caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy collector script and set permissions
COPY collector.py .
RUN chmod +x collector.py

# Switch to non-root user
USER collector

# Default command
ENTRYPOINT ["python", "/app/collector.py"]
CMD ["--help"]
