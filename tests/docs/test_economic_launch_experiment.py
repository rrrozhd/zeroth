from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAUNCH = ROOT / "docs" / "operations" / "economic-debugger-launch-experiment.md"
PILOT = ROOT / "docs" / "operations" / "economic-debugger-commercial-pilot.md"


def test_launch_uses_one_buyer_problem_channel_and_call_to_action() -> None:
    launch = LAUNCH.read_text(encoding="utf-8")

    assert "AI platform engineer" in launch
    assert "Head of AI Platform or FinOps owner" in launch
    assert "Show HN: Zeroth – reconcile AI provider bills to workflow outcomes" in launch
    assert 'pip install "zeroth-core[regulus]"' in launch
    assert "zeroth-econ demo" in launch
    assert "economic-diagnostic-pilot.yml" in launch
    assert "one primary earned channel" in launch
    assert "Do not cross-post during the first 72 hours" in launch


def test_launch_is_gated_on_a_real_release_and_does_not_claim_a_hosted_product() -> None:
    launch = LAUNCH.read_text(encoding="utf-8")

    assert "Do not launch until" in launch
    assert "public PyPI" in launch
    assert "managed service is not implemented" in launch
    assert "not proof of savings" in launch
    assert "Do not ask for upvotes" in launch
    assert "established Hacker News account" in launch


def test_launch_metrics_use_explicit_artifacts_not_downloads_as_activation() -> None:
    launch = LAUNCH.read_text(encoding="utf-8")
    pilot = PILOT.read_text(encoding="utf-8")

    assert "PyPI downloads are exposure, not activation" in launch
    assert "highest artifact produced" in launch
    assert "discovery source" in launch
    assert "one qualified request" in launch
    assert "100 credible installs" not in pilot
    assert "20 real closure reports" in pilot
