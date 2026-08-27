"""Exercise evidence secret rejection without retaining the seeded values."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .evidence import EvidenceStore, UnsafeEvidenceError


def _rejected(callback) -> bool:
    try:
        callback()
    except UnsafeEvidenceError:
        return True
    return False


def main() -> int:
    with TemporaryDirectory(prefix="zeroth-evidence-rejection-") as temporary:
        root = Path(temporary)
        store = EvidenceStore(root / "bundle")
        structured = _rejected(
            lambda: store.append_event(
                "campaign.probe",
                {"authorization": "Bearer " + "seeded-value-that-must-not-survive"},
            )
        )

        source = root / "unsafe.txt"
        source.write_text("Authorization: Bearer " + "seeded-artifact-value")
        artifact = _rejected(
            lambda: store.ingest_artifact(source, "screenshots/rejected.txt")
        )
        artifact_absent = not (store.root / "screenshots/rejected.txt").exists()

        tampered = EvidenceStore(root / "tampered")
        network = tampered.root / "network"
        network.mkdir()
        (network / "unsafe.json").write_text(
            json.dumps({"provider_key": "seeded-provider-value"})
        )
        recursive = _rejected(tampered.write_checksums)
        seal_absent = not tampered.is_sealed

    result = {
        "artifact_destination_absent": artifact_absent,
        "artifact_ingestion_rejected": artifact,
        "recursive_scan_rejected": recursive,
        "sealed_manifest_absent_after_rejection": seal_absent,
        "structured_sensitive_field_rejected": structured,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
