from __future__ import annotations
import os
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Template, RuleChunk
from app.services.ingest_rulebook import ingest_pdf
from app.services.zones import suggest_zones

def bootstrap(db: Session):
    data_dir = settings.DATA_DIR
    if not os.path.isdir(data_dir):
        return

    # templates
    # Skip docx auto-seeding entirely when the curated blueprint templates already exist.
    # Those templates (GOI Letter, DO Letter, etc.) are seeded by migrations and must not
    # be crowded out by stale docx filenames on every container restart.
    _CURATED_NAMES = {"GOI Letter", "DO Letter", "Movement Order", "Leave Certificate", "Service Letter"}
    if db.query(Template).filter(Template.name.in_(_CURATED_NAMES)).count() >= len(_CURATED_NAMES):
        pass  # curated set present — skip docx scan
    else:
      tdir = os.path.join(data_dir, "templates")
      if os.path.isdir(tdir):
        for fn in os.listdir(tdir):
            if not fn.lower().endswith(".docx"):
                continue
            path = os.path.join(tdir, fn)
            # Deduplicate by filename so path-format variations don't cause re-registration
            if db.query(Template).filter(Template.name == fn).first():
                continue

            name = fn
            # better heuristic mapping for your known files
            low = fn.lower()
            low_stem = os.path.splitext(fn)[0].lower()  # without extension
            if "leave certificate" in low or "leave_certificate" in low:
                doc_type = "LEAVE_CERTIFICATE"
            elif "movement" in low and "order" in low:
                doc_type = "MOVEMENT_ORDER"
            elif "do_letter" in low_stem or ("do" in low_stem and "letter" in low_stem and "goi" not in low_stem):
                doc_type = "DO_LETTER"
            elif "goi" in low_stem and "letter" in low_stem:
                doc_type = "GOI_LETTER"
            elif "letter format" in low or "og" in low:
                doc_type = "GENERAL_LETTER"
            else:
                doc_type = os.path.splitext(fn)[0].upper().replace(" ", "_")

            # Skip _markers_ templates — superseded by proper _docxtpl_ versions.
            if "_markers" in low:
                continue

            # Skip non-docxtpl templates when a docxtpl version already exists for this doctype.
            is_docxtpl = "_docxtpl" in low
            if not is_docxtpl:
                has_docxtpl = db.query(Template).filter(
                    Template.doc_type == doc_type,
                    Template.name.op("LIKE")("%_docxtpl%"),
                ).first() or db.query(Template).filter(
                    Template.doc_type == doc_type,
                    Template.name.op("LIKE")("%_markers%"),
                ).first()
                if has_docxtpl:
                    continue

            zones = suggest_zones(path)
            db.add(Template(name=name, doc_type=doc_type, docx_path=path, zones_json=zones))
        db.commit()

    # rulebook
    rulebook = os.path.join(data_dir, settings.RULEBOOK_FILENAME)
    if os.path.exists(rulebook):
        has_any = db.query(RuleChunk).limit(1).first() is not None
        if not has_any:
            ingest_pdf(db, rulebook)
