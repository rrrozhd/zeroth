FROM python:3.12.13-slim-bookworm

RUN pip install --no-cache-dir mcp==1.27.0
COPY tests/runtime/mcp_fixtures/echo_server.py /opt/zeroth/echo_server.py
USER 65534:65534
