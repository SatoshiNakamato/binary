# Production image for the live Flybrain service on Voroa.
#
# The image contains the real male CNS connectome-derived sparse graph and
# headless Chromium. The browser has no wallet, private key, login session or
# trading credentials. FLY_BACKROOM, when enabled, is paper-only.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLY_ALLOW_BROWSER=1 \
    FLY_HOST=0.0.0.0 \
    FLY_TUNNEL=0

WORKDIR /app

COPY requirements-roam.txt requirements-executor.txt ./
RUN pip install -r requirements-roam.txt -r requirements-executor.txt \
    && python -m playwright install --with-deps chromium

# Runtime source/data. prepare_connectome.py downloads the official Janelia
# Male CNS v1.0 Feather release only when build/graph.npz is not already here,
# runs the real build_graph.py, verifies graph.npz exists, then removes the raw
# connectome tables so the final image does not retain the large edge dataset.
COPY build/ build/
COPY data/ data/
COPY door/ door/
COPY *.py ./
COPY web/ web/
COPY voice_prompt.md ./

RUN python prepare_connectome.py

# Voroa supplies PORT=3000. roam.py receives it through run_all.py and binds
# 0.0.0.0 so the public HTTP/WebSocket service is reachable.
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','3000') + '/healthz', timeout=4)" || exit 1

CMD ["python", "run_all.py"]
