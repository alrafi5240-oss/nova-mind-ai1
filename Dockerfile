# The API server. Task sandboxes are sibling containers started through the
# host's Docker daemon, so mount /var/run/docker.sock (see README).
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git docker.io \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nova_agent ./nova_agent
ENV NOVA_DATA_DIR=/data
EXPOSE 8787
CMD ["python", "-m", "nova_agent", "--host", "0.0.0.0", "--port", "8787"]
