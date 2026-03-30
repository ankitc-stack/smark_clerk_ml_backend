from app.db import engine, Base
from app.models import Template, RuleChunk, Document, DocumentVersion

def init_db():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("Tables created successfully.")

if __name__ == "__main__":
    init_db()
