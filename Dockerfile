FROM python:3.11-slim

# Hugging Face Spaces runs Docker Space containers as uid 1000 — create that
# user up front so file ownership is right before anything gets copied in.
RUN useradd -m -u 1000 user

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860

# Must match the `app_port` declared in the README.md Space metadata.
EXPOSE 7860

# Applies any pending migration on every start (safe/idempotent — alembic
# only runs what hasn't already been applied) before serving traffic.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
