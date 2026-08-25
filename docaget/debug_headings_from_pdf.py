
from docagent.extract import extract
from docagent.chunk import DocRules, split_paragraphs, heading_level, ChunkRules

PDF_FILE = "libros/3. CARLOMAGNO Y EL SACRO IMPERIO ROMANO, CISMA DE ORIENTE, CRUZADAS, INQUISICIÓN Y DESARROLLO TEOLÓGICO MEDIEVAL..pdf"

# This mimics the n_validate node with default rules
extracted = extract(PDF_FILE, DocRules(), "pdf_text")
text = extracted.text if extracted.text is not None else b""
paras = split_paragraphs(text)

rules = ChunkRules()

print(f"Analyzing {len(paras)} paragraphs from {PDF_FILE} with default rules...")

for p in paras:
    level = heading_level(p.text, rules)
    if level > 0:
        print(f"  Level {level}: {p.text}")

