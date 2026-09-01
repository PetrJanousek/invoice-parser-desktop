"""Compare the old blank-string routing heuristic against pdf-inspector.

Run it on real documents to see which pages would be sent to the (paid) vision
OCR path and why:

    PYTHONPATH=. .venv/bin/python scripts/inspect_pdf.py tests/fixtures/real_documents/*.pdf
    PYTHONPATH=. .venv/bin/python scripts/inspect_pdf.py tests/fixtures/real_documents

A directory argument is scanned for *.pdf. Classification runs fully offline —
no API keys and no LLM calls. Pass --extract to additionally run the real
extraction path, which DOES call the configured vision model for every page
routed to OCR.
"""
import argparse
import sys
from pathlib import Path

import fitz

from app import extract


def _pdf_paths(args: list[str]) -> list[Path]:
    paths = []
    for a in args:
        p = Path(a)
        paths.extend(sorted(p.glob("*.pdf")) if p.is_dir() else [p])
    return paths


def _report(path: Path, run_extract: bool) -> None:
    pdf = path.read_bytes()
    print(f"\n=== {path.name} ===")

    try:
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            raw = [page.get_text() for page in doc]
    except Exception as exc:
        print(f"  could not read: {exc}")
        return

    try:
        result = extract.pdf_inspector.classify_pdf_bytes(pdf)
    except Exception as exc:
        print(f"  pdf-inspector failed: {exc} (routing falls back to the blank check)")
        result = None

    needs_ocr = extract._pages_needing_ocr(pdf)
    if result is not None:
        print(f"  type={result.pdf_type}  confidence={result.confidence:.2f}  "
              f"pages={result.page_count}")
        if result.confidence < extract._MIN_CLASSIFY_CONFIDENCE:
            print("  low confidence -> every page routed to OCR")

    print(f"  {'page':>4}  {'chars':>6}  {'old':>9}  {'pdf-inspector':>13}  agree")
    disagreements = 0
    for i, text in enumerate(raw):
        old_ocr = not text.strip()
        new_ocr = i in needs_ocr
        agree = old_ocr == new_ocr
        disagreements += not agree
        print(f"  {i:>4}  {len(text.strip()):>6}  "
              f"{'OCR' if old_ocr else 'text':>9}  {'OCR' if new_ocr else 'text':>13}  "
              f"{'yes' if agree else 'NO'}")

    print(f"  -> old would OCR {sum(1 for t in raw if not t.strip())} page(s), "
          f"pdf-inspector routes {len(needs_ocr)}; {disagreements} disagreement(s)")

    if run_extract:
        try:
            pages = extract.pdf_to_pages(pdf)
        except Exception as exc:
            print(f"  extraction failed: {exc}")
            return
        print(f"  extracted {len(pages)} non-empty page(s); "
              f"{len(needs_ocr)} went through the vision model")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="PDF files or directories")
    ap.add_argument("--extract", action="store_true",
                    help="also run the real extraction (needs a reachable vision model)")
    args = ap.parse_args()

    paths = _pdf_paths(args.paths)
    if not paths:
        print("No PDFs found.", file=sys.stderr)
        return 1
    for path in paths:
        _report(path, args.extract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
