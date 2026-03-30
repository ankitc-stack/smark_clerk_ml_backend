from __future__ import annotations
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import RuleChunk
from app.providers.embedding_provider import embed

import numpy as np
from sqlalchemy import inspect

def search_rules(db: Session, query: str, doc_type: str, k: int = 6) -> list[RuleChunk]:
    q = embed(query)
    
    # Check if we are using PostgreSQL with pgvector
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        stmt = (
            select(RuleChunk)
            .where(RuleChunk.doc_type.in_([doc_type, "GENERAL_RULES"]))
            .order_by(RuleChunk.embedding.cosine_distance(q))
            .limit(k)
        )
        return db.execute(stmt).scalars().all()
    else:
        # Fallback for SQLite: Get all rules and rank them manually
        stmt = select(RuleChunk).where(RuleChunk.doc_type.in_([doc_type, "GENERAL_RULES"]))
        all_chunks = db.execute(stmt).scalars().all()
        
        def cosine_sim(a, b):
            return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
        
        # Rank by similarity (descending)
        ranked = sorted(all_chunks, key=lambda c: cosine_sim(c.embedding, q), reverse=True)
        return ranked[:k]
