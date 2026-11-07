# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN useradd --system --uid 10001 --create-home --home-dir /opt/credibly credibly

WORKDIR /opt/credibly/dashboard

COPY requirements.txt /tmp/dashboard-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/dashboard-requirements.txt

COPY --chown=10001:10001 . /opt/credibly/dashboard
RUN python -m compileall -q /opt/credibly/dashboard

USER 10001:10001

EXPOSE 8501

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]

CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
