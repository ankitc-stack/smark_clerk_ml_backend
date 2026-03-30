from docx import Document
import sys

def inspect_docx(filepath, output_text):
    doc = Document(filepath)
    with open(output_text, 'w', encoding='utf-8') as f:
        for i, p in enumerate(doc.paragraphs):
            align = p.alignment
            # 0=Left, 1=Center, 2=Right, None=Left(default)
            align_str = str(align) if align is not None else "None (Left)"
            f.write(f"{i}: [{align_str}] {p.text}\n")
    print(f"Inspection written to {output_text}")

if __name__ == "__main__":
    inspect_docx('data/demi_official_letter_v6.docx', 'data/inspect_v6.txt')
