import fitz
from app.bank_parsers import _page_rows
# Rows with slight vertical drift (typical of scanned/deskewed pages):
# each word nudged down 2pt across the line - within _ROW_TOL of its neighbour
# but cumulatively 8pt from the anchor.
doc=fitz.open(); p=doc.new_page()
for i,w in enumerate(["01.06.2026","Platba","DODAVATEL","9988","-500,00"]):
    p.insert_text((45+i*110, 200+i*2.0), w, fontsize=9, fontname="helv")
b=doc.tobytes()
pg=fitz.open(stream=b,filetype="pdf")[0]
rows=_page_rows(pg)
print("rows found (ideal=1):", len(rows))
for y,cells in rows: print(f"  y={y:.1f} -> {[w for _,w in cells]}")
