"""A plain-SDK worker (the C07 Python path) that keeps its own log of delivered events.

Usage: lifecycle_worker.py URL KEY WORKLOAD_PY VERSION LOG_PATH [STOP_AFTER]
Delivers the frozen reference workload; every acknowledged event id is appended
to LOG_PATH before the next delivery. With STOP_AFTER the process exits hard
(``os._exit(17)``) right after that many deliveries, like a crashed container.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def main() -> int:
    url, key, workload_path, version, log_path = sys.argv[1:6]
    stop_after = int(sys.argv[6]) if len(sys.argv) > 6 else -1
    from zeroth.instrumentation import Recorder
    from zeroth.sdk import ZerothClient

    spec = importlib.util.spec_from_file_location("reference_workload", workload_path)
    workload = importlib.util.module_from_spec(spec)
    sys.modules["reference_workload"] = workload
    spec.loader.exec_module(workload)

    class Logged:
        """Acknowledged deliveries are written durably before the worker continues."""

        def __init__(self, inner):
            self.inner, self.delivered = inner, 0

        def _note(self, event, response):
            self.delivered += 1
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps({"event_id": getattr(event, "event_id", None) or "outcome",
                                      "status": response["status"]}) + "\n")
            if stop_after >= 0 and self.delivered >= stop_after:
                os._exit(17)
            return response

        def record_execution(self, event):
            return self._note(event, self.inner.record_execution(event))

        def record_outcome(self, event):
            return self._note(event, self.inner.record_outcome(event))

    logged = Logged(ZerothClient(api_key=key, base_url=url))
    workload.replay(Recorder(logged), version)
    print(json.dumps({"delivered": logged.delivered}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
