import fitz
from app import extract

# Build a PDF the blank-string heuristic gets WRONG: page 1 is genuine text,
# page 2 is a pure scan that carries one tiny footer watermark ("Strana 2 / 2").
# PyMuPDF returns that footer, so the page is not blank and the old heuristic
# trusts it — silently dropping the entire transaction body. pdf-inspector
# classifies the page as image_based and routes it to OCR.
doc = fitz.open()
p = doc.new_page()
p.insert_text((60, 80), "Vypis z uctu c. 7 / strana 1", fontsize=12)
p.insert_text((60, 100), "Pocatecni zustatek: 4 000,00", fontsize=12)
p.insert_text((60, 120), "Konecny zustatek: 1 550,00", fontsize=12)

# The scan: text rendered to a pixmap, so none of it survives as embedded text.
tmp = fitz.open()
tp = tmp.new_page()
tp.insert_text((60, 80), "TRANSAKCE STRANA 2", fontsize=14)
for i in range(6):
    tp.insert_text((60, 120 + i*20), f"0{i+1}.07.2026  PLATBA KARTOU  -1 2{i}0,00", fontsize=11)
pix = tp.get_pixmap(dpi=144)
np = doc.new_page(width=tp.rect.width, height=tp.rect.height)
np.insert_image(np.rect, pixmap=pix)
np.insert_text((250, np.rect.height - 12), "Strana 2 / 2", fontsize=6)

pdf = doc.tobytes()
open("tests/fixtures/watermarked_scan_page.pdf", "wb").write(pdf)

raw = extract.extract_text_pages(pdf)
print("per-page embedded text:", [repr(t.strip()[:40]) for t in raw])
print("old heuristic would OCR:", [i for i, t in enumerate(raw) if not t.strip()])
print("pdf-inspector says OCR:", sorted(extract._pages_needing_ocr(pdf)))
