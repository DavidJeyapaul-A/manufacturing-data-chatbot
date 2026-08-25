#!/bin/sh
# One-shot model pull. Runs in the `ollama-init` service.
#
# OLLAMA_HOST points at the ollama SERVICE, so this container acts purely as a
# client: the server does the downloading and stores the blobs in its own named
# volume. That means the model is pulled exactly once, ever — every later
# `docker compose up` finds it already present and this script exits in <1s.
set -eu

MODEL="${OLLAMA_MODEL:?OLLAMA_MODEL is not set}"
HOST="${OLLAMA_HOST:-http://ollama:11434}"

echo "[ollama-init] server   : ${HOST}"
echo "[ollama-init] model    : ${MODEL}"

# ---------------------------------------------------------------- wait for API
# compose already gates us on the healthcheck, but a container can be healthy a
# moment before it accepts its first request. 60 x 2s = 2 minutes of patience.
attempt=1
max_attempts=60
until ollama list >/dev/null 2>&1; do
    if [ "${attempt}" -ge "${max_attempts}" ]; then
        echo "[ollama-init] FAILED: no response from ${HOST} after $((max_attempts * 2))s." >&2
        echo "[ollama-init] Check:  docker compose logs ollama" >&2
        exit 1
    fi
    echo "[ollama-init] waiting for the ollama server... (${attempt}/${max_attempts})"
    attempt=$((attempt + 1))
    sleep 2
done
echo "[ollama-init] server is responding."

# ------------------------------------------------------- pull only if missing
# `ollama list` prints "NAME" in a header row plus one row per model. Match the
# exact name in the first column so 'qwen2.5-coder:7b' doesn't match ':7b-foo'.
if ollama list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx "${MODEL}"; then
    echo "[ollama-init] '${MODEL}' is already in the volume — nothing to download."
else
    echo "[ollama-init] '${MODEL}' not found. Pulling (this is the ONLY step that"
    echo "[ollama-init] needs the internet; several GB, expect a few minutes)..."
    if ! ollama pull "${MODEL}"; then
        echo "[ollama-init] FAILED: could not pull '${MODEL}'." >&2
        echo "[ollama-init] Is the host online? Is the model name a real tag?" >&2
        echo "[ollama-init] Browse tags at https://ollama.com/library" >&2
        exit 1
    fi
    echo "[ollama-init] pull complete."
fi

# ------------------------------------------------------------------- verify
# Presence in `list` is not proof it loads. Confirm the model file is readable
# and well-formed before we let the app start.
if ! ollama show "${MODEL}" >/dev/null 2>&1; then
    echo "[ollama-init] FAILED: '${MODEL}' is listed but 'ollama show' failed." >&2
    echo "[ollama-init] The download may be corrupt. Try:" >&2
    echo "[ollama-init]   docker compose run --rm ollama-init ollama rm ${MODEL}" >&2
    exit 1
fi

echo "[ollama-init] '${MODEL}' is ready. Exiting 0 so the app can start."
