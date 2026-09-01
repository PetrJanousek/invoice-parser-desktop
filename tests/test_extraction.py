"""Extraction eval as a pytest test.

Runs the eval cases through the configured LLM and asserts overall field
accuracy stays above a floor. Skipped automatically if the LLM backend isn't
reachable (e.g. Ollama not running), so CI without a model doesn't fail.

Tune ACCURACY_FLOOR as the model/prompt improves. With local qwen2.5:7b we see
~0.90+; cloud Haiku should be higher.
"""
import pytest

from app.config import settings
from evals.run_evals import load_cases, run_case

ACCURACY_FLOOR = 0.80


def _backend_reachable() -> bool:
    if settings.llm_provider == "ollama":
        import urllib.request

        try:
            urllib.request.urlopen(settings.ollama_url + "/api/tags", timeout=2)
            return True
        except Exception:
            return False
    # Cloud providers: only run if a key is present.
    if settings.llm_provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if settings.llm_provider == "openai":
        return bool(settings.openai_api_key)
    return False


@pytest.mark.skipif(
    not _backend_reachable(), reason="LLM backend not reachable/configured"
)
def test_extraction_accuracy_above_floor():
    cases = load_cases()
    reports = [run_case(c) for c in cases]
    passed = sum(r["passed"] for r in reports)
    total = sum(r["total"] for r in reports)
    accuracy = passed / total
    detail = "\n".join(f"  {r['name']}: {r['accuracy']:.0%}" for r in reports)
    assert accuracy >= ACCURACY_FLOOR, (
        f"Extraction accuracy {accuracy:.1%} below floor {ACCURACY_FLOOR:.0%}\n"
        + detail
    )
