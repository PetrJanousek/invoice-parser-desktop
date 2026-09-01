import fitz
from app import extract, bank_parsers

# Build a "mixed" PDF: page 1 has real text (header), pages 2-3 are image-only
# (rendered from text then pasted as a pixmap) - i.e. a scanned statement whose
# cover page was generated digitally.
doc = fitz.open()
p = doc.new_page()
p.insert_text((60, 80), "Vypis z uctu / Vypis z uctu c. 1", fontsize=12)
p.insert_text((60, 100), "Pocatecni zustatek: 1 000,00", fontsize=12)
p.insert_text((60, 120), "Konecny zustatek: 2 500,00", fontsize=12)

# make two image-only pages
tmp = fitz.open()
for n in (1, 2):
    tp = tmp.new_page()
    tp.insert_text((60, 80), f"TRANSAKCE STRANA {n}", fontsize=14)
    for i in range(5):
        tp.insert_text((60, 120 + i*20), f"0{i+1}.06.2026  PLATBA {n}-{i}  -1 234,50", fontsize=11)
for tp in tmp:
    pix = tp.get_pixmap(dpi=72)
    np = doc.new_page(width=tp.rect.width, height=tp.rect.height)
    np.insert_image(np.rect, pixmap=pix)
pdf = doc.tobytes()
open("tests/fixtures/mixed_text_and_scan_statement.pdf","wb").write(pdf)

raw = extract.extract_text_pages(pdf)
print("total pages in PDF:", len(raw))
print("per-page text lengths:", [len(t.strip()) for t in raw])
pages = extract._pdf_to_pages(pdf)
print(">>> pages returned by _pdf_to_pages:", len(pages), "  (PAGES DROPPED:", len(raw)-len(pages), ")")
full = "\n".join(pages)
print("looks_like_bank_statement:", bank_parsers.looks_like_bank_statement(full))
print("text the LLM would see:", repr(full[:200]))
