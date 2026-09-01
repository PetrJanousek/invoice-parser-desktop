"""Extraction eval harness.

Loads each JSON case in evals/cases/, runs the real extraction path
(``extract_fields_from_text`` -> configured LLM), and scores the result
field-by-field against expected values with lenient, format-tolerant matching.

Run directly:   uv run python evals/run_evals.py
Via pytest:     uv run pytest tests/test_extraction.py

Uses whatever LLM_PROVIDER points at (default: local Ollama).
"""
import json
import re
import sys
from pathlib import Path

# Make ``app`` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extract import extract_fields_from_text  # noqa: E402
from app.llm import active_model_label  # noqa: E402
from app.schemas import FIELDS  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent / "cases"

AMOUNT_FIELDS = {"subtotal", "tax", "total"}
DIGIT_FIELDS = {"ico", "variable_symbol"}


def _as_number(value) -> float | None:
    """Parse a messy amount string ('50 000', '1,234.56', '975,80') to float."""
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Single comma: decimal separator if it looks like one, else thousands.
        if len(s.split(",")[-1]) == 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value))


def _norm(value) -> str:
    return str(value).strip().lower().rstrip(".")


def field_matches(field: str, expected, actual) -> bool:
    """Lenient per-field comparison. ``expected`` may be None, a str, or a
    list of acceptable strings."""
    # Expected null -> actual must be empty/null.
    if expected is None:
        return actual is None or str(actual).strip() == ""
    if actual is None or str(actual).strip() == "":
        return False

    options = expected if isinstance(expected, list) else [expected]

    for exp in options:
        if field in AMOUNT_FIELDS:
            ea, aa = _as_number(exp), _as_number(actual)
            if ea is not None and aa is not None and abs(ea - aa) < 0.01:
                return True
        elif field in DIGIT_FIELDS:
            if _digits(exp) and _digits(exp) == _digits(actual):
                return True
        elif field == "bank_account":
            if _norm(exp).replace(" ", "") == _norm(actual).replace(" ", ""):
                return True
        elif field == "currency":
            if _norm(exp) == _norm(actual) or _norm(exp) in _norm(actual):
                return True
        elif field == "vendor":
            en, an = _norm(exp), _norm(actual)
            if en == an or en in an or an in en:
                return True
        else:
            if _norm(exp) == _norm(actual):
                return True
    return False


def run_case(case: dict) -> dict:
    actual = extract_fields_from_text(case["text"])
    expected = case["expected"]
    results = {}
    for f in FIELDS:
        ok = field_matches(f, expected.get(f), actual.get(f))
        results[f] = {"ok": ok, "expected": expected.get(f), "actual": actual.get(f)}
    passed = sum(1 for r in results.values() if r["ok"])
    return {
        "name": case["name"],
        "results": results,
        "passed": passed,
        "total": len(FIELDS),
        "accuracy": passed / len(FIELDS),
    }


def load_cases() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CASES_DIR.glob("*.json"))]


def main() -> int:
    print(f"Extraction evals — model: {active_model_label()}\n")
    cases = load_cases()
    case_reports = []
    for case in cases:
        report = run_case(case)
        case_reports.append(report)
        print(f"[{report['accuracy']:.0%}] {report['name']} "
              f"({report['passed']}/{report['total']} fields)")
        for f, r in report["results"].items():
            if not r["ok"]:
                print(f"      ✗ {f}: expected={r['expected']!r} got={r['actual']!r}")

    overall = sum(r["passed"] for r in case_reports) / sum(
        r["total"] for r in case_reports
    )
    print(f"\nOverall field accuracy: {overall:.1%} "
          f"across {len(case_reports)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
