# Autonomous PentPort runner -- deploy to a server YOU control.
# The live API key is provided at runtime via the PENTPORT_API_KEY env var
# (never baked into the image). See deploy/DEPLOY.md.
FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code (the .dockerignore keeps .env, .venv, logs, state, .git out)
COPY pp_options/ ./pp_options/
COPY autorun.py .

# unprivileged user; persistent dirs for logs + the position ledger
RUN useradd -m -u 10001 trader \
    && mkdir -p /app/logs /app/state \
    && chown -R trader:trader /app
USER trader

ENV PYTHONUNBUFFERED=1

# Default = LIVE autonomous daemon (per request). The runner self-gates to market
# hours, runs the protective-leg-first executor, the 3%/20% guardrails, and the
# daily-loss kill switch. Override to PAPER with:  ... python autorun.py --daemon
CMD ["python", "autorun.py", "--live", "--daemon"]
