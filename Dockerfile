# Payment Operations Intelligence: one image for the API, the dashboard and the command-line tools.
#
#   docker compose --profile app build
#
# * Base: the official Python 3.11 slim image (Debian bookworm), the same Python as development.
#   For fully repeatable builds, pin it by digest after the first pull:
#     docker images --digests python   ->   FROM python:3.11-slim-bookworm@sha256:<digest>
# * Packages: exactly the versions in requirements.lock (pip freeze from a machine where all
#   tests passed). All are prebuilt wheels, so no compiler is installed.
# * Runs as an unprivileged user (uid 10001), never as root.
# * Telemetry switched off for Streamlit and LangSmith; pip's version check off.
# * Contains no secrets and no data: passwords arrive as environment variables at run time,
#   and .dockerignore keeps .env, data/ and .git out of the build.

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false

WORKDIR /app

# Dependencies first: this layer is reused until requirements.lock changes.
# The lock is written on Windows by pip freeze; the project itself (payops) is installed below,
# so a line for it is skipped. If a Windows-only package ever appears in the lock (e.g. pywin32),
# the build stops at this step naming it: add it to the grep pattern.
COPY requirements.lock ./
RUN grep -viE '^(payops)([ =@]|$)' requirements.lock > /tmp/requirements.txt \
 && pip install --root-user-action=ignore -r /tmp/requirements.txt

# The application itself (no dependencies: they are all installed above).
COPY pyproject.toml README.md ./
COPY src ./src
COPY .streamlit ./.streamlit
RUN pip install --root-user-action=ignore --no-deps . \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin payops \
 && mkdir -p /app/data /app/evaluation/results \
 && chown -R payops:payops /app/data /app/evaluation

USER payops

EXPOSE 8000 8501

# Default: the API. docker-compose.yml starts the dashboard and tools with other commands.
# 0.0.0.0 is the container's own network card; docker-compose.yml publishes the port on the
# host's 127.0.0.1 only, so nothing outside this computer can connect.
CMD ["uvicorn", "payops.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
