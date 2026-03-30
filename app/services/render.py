from __future__ import annotations
import os
import zipfile
from docxtpl import DocxTemplate

from docx import Document
import re


def _fix_richtext_runs(docx_path: str) -> None:
    """Unwrap RichText XML that docxtpl buried inside <w:t> nodes for loop variables.

    docxtpl pre-processes {{ var }} for RichText only on top-level template variables.
    When {{ p }} is inside a {% for %} loop, docxtpl cannot pre-process it, so it
    renders the RichText's XML string as literal text content inside <w:t>. The XML
    parser sees those as real child <w:r> elements nested inside <w:t>, which Word
    ignores. This function hoists them up to be siblings of the outer <w:r>, restoring
    bold/italic/underline formatting in the final DOCX.
    """
    try:
        from lxml import etree
    except ImportError:
        return  # lxml not available — skip silently

    W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    with zipfile.ZipFile(docx_path, 'r') as z:
        all_files = {name: z.read(name) for name in z.namelist()}

    root = etree.fromstring(all_files['word/document.xml'])
    body = root.find(f'.//{{{W}}}body')
    if body is None:
        return

    changed = False
    for para in body.iter(f'{{{W}}}p'):
        for r_elem in list(para.findall(f'{{{W}}}r')):
            t_elem = r_elem.find(f'{{{W}}}t')
            if t_elem is None:
                continue
            inner_rs = t_elem.findall(f'{{{W}}}r')
            if not inner_rs:
                continue
            # Outer <w:r> wraps inner <w:r> elements inside <w:t> — unwrap them
            parent = r_elem.getparent()
            if parent is None:
                continue
            idx = list(parent).index(r_elem)
            parent.remove(r_elem)
            for i, inner_r in enumerate(inner_rs):
                parent.insert(idx + i, inner_r)
            changed = True

    if changed:
        all_files['word/document.xml'] = etree.tostring(
            root, xml_declaration=True, encoding='UTF-8', standalone=True
        )
        with zipfile.ZipFile(docx_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in all_files.items():
                z.writestr(name, data)


def render_docx(template_path: str, doc_state: dict, out_path: str):
    """Renders using docxtpl placeholders."""
    context = {}
    fields = doc_state.get("fields", {})
    lists = doc_state.get("lists", {})
    blocks = doc_state.get("blocks", {})

    context.update(fields)
    context.update(lists)
    context.update(blocks)

    # Ensure SUBJECT_UPPER and other fallbacks are in context
    if "SUBJECT" in fields and "SUBJECT_UPPER" not in context:
        context["SUBJECT_UPPER"] = fields["SUBJECT"].upper()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Use docxtpl (Jinja2) - this is the safest way to preserve formatting
    tpl = DocxTemplate(template_path)
    tpl.render(context)
    tpl.save(out_path)

    # Fix RichText formatting that docxtpl buries inside <w:t> for loop variables
    _fix_richtext_runs(out_path)
