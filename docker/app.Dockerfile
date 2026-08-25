# FastAPI backend + static UI. Also used by the one-shot `seed` service.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# Dependencies first so edits to source don't invalidate the pip layer.
# psycopg[binary] ships wheels, so no build-essential / libpq-dev needed.
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Baked in so the image runs standalone; compose bind-mounts ./app over the
# top of this at runtime for --reload.
COPY app/ /srv/app/

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
