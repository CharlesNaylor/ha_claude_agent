FROM python:3.12-slim

# The Claude Agent SDK bundles the Claude Code CLI, which requires Node >= 18.
# python:3.12-slim is Debian bookworm, whose nodejs package is >= 18.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8088

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088"]
