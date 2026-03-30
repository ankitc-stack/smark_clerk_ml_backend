import argparse
from app.db import SessionLocal
from app.services.ingest_rulebook import ingest_pdf

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    args = ap.parse_args()
    db = SessionLocal()
    ingest_pdf(db, args.pdf)
    print("Ingested rulebook:", args.pdf)

if __name__ == "__main__":
    main()
