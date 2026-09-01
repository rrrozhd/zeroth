from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "how-to" / "economic-debugger.md"


def test_real_activation_uses_the_authenticated_shipped_client() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "InstrumentationClient" in guide
    assert "InstrumentationClient.authenticated" in guide
    assert 'bearer_token=os.environ["ZEROTH_ECON_TOKEN"]' in guide
    assert "track_execution_confirmed" in guide
    assert "track_outcome_confirmed" in guide
    assert "accepted before returning" in guide
    assert guide.index("InstrumentationClient") < guide.index(
        "Equivalent HTTP contract"
    )


def test_activation_documents_rotating_and_environment_auth_without_exposing_tokens() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "ECP_BEARER_TOKEN" in guide
    assert "headers_provider" in guide
    assert "Choose one authentication path" in guide
    assert "Do not put the token in source code" in guide
