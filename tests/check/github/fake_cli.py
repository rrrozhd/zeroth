from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    report_dir = Path(sys.argv[sys.argv.index("--report-dir") + 1])
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "check-summary.md").write_text("# one summary\n")
    return int(os.environ["FAKE_CHECK_EXIT"])


if __name__ == "__main__":
    raise SystemExit(main())
