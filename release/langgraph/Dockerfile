# The upstream the deployed acceptance stack governs: the real LangGraph Agent Server,
# pinned to the version the gateway's compatibility fingerprint was taken from, serving
# the shell application in release/langgraph/shell_graph.py.
#
# Pinned exactly, not ranged. The gateway compares the upstream's OpenAPI fingerprint
# against a module constant, so a floating version here would surface as an unexplained
# "unsupported" rather than as the dependency change it actually is.
FROM python:3.12.13-slim-bookworm

WORKDIR /workspace

RUN pip install --no-cache-dir \
      "langgraph-api==0.11.1" \
      "langgraph==1.2.9" \
      "langgraph-sdk==0.4.2" \
      "langchain==1.3.14" \
      "langgraph-checkpoint-sqlite>=3.0,<4"

COPY release/langgraph/shell_graph.py /workspace/release/langgraph/shell_graph.py
RUN touch /workspace/release/__init__.py /workspace/release/langgraph/__init__.py

ENV PYTHONPATH=/workspace \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    LANGGRAPH_CLOUD_LICENSE_KEY=""

EXPOSE 8123

CMD ["python", "-c", "from langgraph_api.cli import run_server; run_server(host='0.0.0.0', port=8123, reload=False, graphs={'shell': 'release.langgraph.shell_graph:graph'}, disable_persistence=True, open_browser=False, server_level='ERROR')"]
