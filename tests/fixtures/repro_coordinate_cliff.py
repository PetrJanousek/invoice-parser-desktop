import fitz
from app import bank_parsers as bp

def build(shift=0, dia=True):
    doc = fitz.open(); p = doc.new_page()
    T = lambda s: s if dia else (s.replace("ý","y").replace("ú","u").replace("č","c")
        .replace("Č","C").replace("á","a").replace("é","e").replace("ě","e").replace("š","s").replace("ř","r"))
    hdr=[(40,60,T("Výpis z účtu"),14),(40,75,T("Česká spořitelna"),10),(40,90,"GIBACZPX",10),
         (40,105,T("Číslo účtu/kód banky: 7062814319/0800"),10),(40,118,T("Měna účtu: CZK"),10),
         (40,131,T("Počáteční zůstatek: 10 000,00"),10),(40,144,T("Konečný zůstatek: 8 000,00"),10)]
    for x,y,t,s in hdr: p.insert_text((x,y),t,fontsize=s,fontname="helv")
    for i in range(4):
        y=180+i*18
        for x,t in [(45,f"0{i+1}.06.2026"),(95,"Platba"),(250,"DODAVATEL"),(405,"9988"),(505,"-500,00")]:
            p.insert_text((x+shift,y),t,fontsize=9,fontname="helv")
    return doc.tobytes()

print("--- diacritics sensitivity (shift=0) ---")
for dia in (True, False):
    b=build(0,dia); text="\n".join(pg.get_text() for pg in fitz.open(stream=b,filetype="pdf"))
    st=bp.parse_bank_statement(b,text)
    print(f"  diacritics={dia}: looks_like={bp.looks_like_bank_statement(text)} bank={bp.detect_bank(text)} txns={len(st['transactions']) if st else None}")

print("--- x-offset sensitivity (diacritics on) ---")
for sh in (0,10,20,30,40,50,60,80):
    b=build(sh,True); text="\n".join(pg.get_text() for pg in fitz.open(stream=b,filetype="pdf"))
    st=bp.parse_bank_statement(b,text)
    n=len(st["transactions"]) if st else None
    amt=st["transactions"][0]["amount"] if st and st["transactions"] else None
    print(f"  shift=+{sh:3}pt: txns={str(n):5} first_amount={amt}")
