import argparse, os
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import Template
from app.services.zones import suggest_zones

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates_dir", required=True)
    ap.add_argument("--doc_type", default=None, help="If provided, force doc_type for all templates.")
    args = ap.parse_args()

    db: Session = SessionLocal()
    for fn in os.listdir(args.templates_dir):
        if not fn.lower().endswith(".docx"):
            continue
        path = os.path.join(args.templates_dir, fn)
        doc_type = args.doc_type or os.path.splitext(fn)[0].upper().replace(" ", "_")
        zones = suggest_zones(path)
        t = Template(name=fn, doc_type=doc_type, docx_path=path, zones_json=zones)
        db.add(t)
        db.commit()
        print("Registered:", fn, "as", doc_type)

if __name__ == "__main__":
    main()
