import fitz
from app import bank_parsers as bp

OUT = "tests/fixtures/"

def stmt_pdf(name, lines, width=595, height=842, fontfile=None):
    doc = fitz.open(); p = doc.new_page(width=width, height=height)
    for (x, y, txt, sz) in lines:
        p.insert_text((x, y), txt, fontsize=sz, fontname="helv")
    b = doc.tobytes(); open(OUT+name,"wb").write(b); return b

# 1. Czech CS-style statement but WITHOUT diacritics (what OCR often yields)
nodia = [(40,60,"Vypis z uctu",14),(40,80,"Cislo uctu/kod banky: 7062814319/0800",10),
         (40,95,"Pocatecni zustatek: 10 000,00",10),(40,110,"Konecny zustatek: 12 500,00",10),
         (40,140,"01.06.2026",9),(95,140,"Platba kartou",9),(250,140,"ALZA CZ",9),(405,140,"12345",9),(505,140,"-1 234,50",9)]
b1 = stmt_pdf("cz_statement_no_diacritics.pdf", nodia)

# 2. Proper Czech, but an UNKNOWN bank (Komercni banka)
kb = [(40,60,"Vypis z uctu",14),(40,75,"Komercni banka, a.s.",10),
      (40,95,"Pocatecni zustatek: 5 000,00",10),(40,110,"Konecny zustatek: 4 000,00",10),
      (40,140,"02.06.2026",9),(95,140,"Prevod",9),(250,140,"CEZ Prodej",9),(505,140,"-1 000,00",9)]
b2 = stmt_pdf("kb_unknown_bank_statement.pdf", kb)

# 3. English-language statement
en = [(40,60,"Account Statement",14),(40,80,"Account number: GB29 NWBK 6016 1331 9268 19",10),
      (40,95,"Opening balance: 1,000.00 GBP",10),(40,110,"Closing balance: 850.00 GBP",10),
      (40,140,"01/06/2026",9),(95,140,"Card payment",9),(250,140,"AMAZON UK",9),(505,140,"-150.00",9)]
b3 = stmt_pdf("en_account_statement.pdf", en)

# 4. Ceska sporitelna-like layout but shifted right by 40pt (layout drift / different template)
shift = 40
cs = [(40,60,"Vypis z uctu",14),(40,75,"Ceska sporitelna",10),(40,90,"GIBACZPX",10),
      (40,105,"Cislo uctu/kod banky: 7062814319/0800",10),
      (40,120,"Pocatecni zustatek: 10 000,00",10),(40,135,"Konecny zustatek: 8 000,00",10)]
for i in range(4):
    y = 170+i*18
    cs += [(40+shift,y,f"0{i+1}.06.2026",9),(95+shift,y,"Platba",9),(250+shift,y,"DODAVATEL",9),(405+shift,y,"9988",9),(505+shift,y,"-500,00",9)]
b4 = stmt_pdf("cs_layout_shifted_40pt.pdf", cs)

# 4b. Same, shifted far enough right that every column leaves its expected band
shift = 70
cs70 = [(40,60,"Vypis z uctu",14),(40,75,"Ceska sporitelna",10),(40,90,"GIBACZPX",10),
      (40,105,"Cislo uctu/kod banky: 7062814319/0800",10),
      (40,120,"Pocatecni zustatek: 10 000,00",10),(40,135,"Konecny zustatek: 8 000,00",10)]
for i in range(4):
    y = 170+i*18
    cs70 += [(45+shift,y,f"0{i+1}.06.2026",9),(95+shift,y,"Platba",9),(250+shift,y,"DODAVATEL",9),(405+shift,y,"9988",9),(505+shift,y,"-500,00",9)]
b4b = stmt_pdf("cs_layout_shifted_70pt.pdf", cs70, width=680)  # wider page: the shifted amount column must still fit

# 5. Same but UNSHIFTED (control - should parse)
cs0 = [(40,60,"Vypis z uctu",14),(40,75,"Ceska sporitelna",10),(40,90,"GIBACZPX",10),
      (40,105,"Cislo uctu/kod banky: 7062814319/0800",10),
      (40,120,"Pocatecni zustatek: 10 000,00",10),(40,135,"Konecny zustatek: 8 000,00",10)]
for i in range(4):
    y = 170+i*18
    cs0 += [(45,y,f"0{i+1}.06.2026",9),(95,y,"Platba",9),(250,y,"DODAVATEL",9),(405,y,"9988",9),(505,y,"-500,00",9)]
b5 = stmt_pdf("cs_layout_control.pdf", cs0)

for label, b in [("no_diacritics",b1),("kb_unknown",b2),("english",b3),("cs_shifted",b4),("cs_control",b5)]:
    text = "\n".join(p.get_text() for p in fitz.open(stream=b, filetype="pdf"))
    looks = bp.looks_like_bank_statement(text)
    bank = bp.detect_bank(text)
    parsed = bp.parse_bank_statement(b, text)
    n = len(parsed["transactions"]) if parsed else None
    print(f"{label:15} looks_like={str(looks):5} bank={str(bank):18} parsed_txns={n}")
