from __future__ import annotations
import os, subprocess
from app.config import settings

def docx_to_pdf(docx_path: str, pdf_path: str):
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    outdir = os.path.dirname(pdf_path)
    cmd = [
        settings.LIBREOFFICE_BIN,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", outdir,
        docx_path
    ]
    subprocess.check_call(cmd)
    produced = os.path.join(outdir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    if produced != pdf_path:
        os.replace(produced, pdf_path)
