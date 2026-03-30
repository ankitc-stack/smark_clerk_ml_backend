import os
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import Template, RuleChunk
from app.services.rag import search_rules
from app.services.render import render_docx
from app.providers.llm_provider import generate_doc_state
from app import crud

def test_rag_gen():
    db: Session = SessionLocal()
    
    # 1. RAG Query
    prompt = "write a demi official letter to Major General Vikram Sen Chauhan regarding coordination for the upcoming Vijay Diwas Parade"
    print(f"Prompt: {prompt}")
    
    # Search for rules related to DO letters
    chunks = search_rules(db, prompt, "REFINED_DO_LETTER", k=3)
    print("\n--- RAG Results (Rules) ---")
    for i, c in enumerate(chunks):
        print(f"[{i+1}] Page {c.page_start}: {c.text[:200]}...")
    
    rules_text = "\n\n".join([c.text for c in chunks])
    
    # 2. Identify Template
    template = db.query(Template).filter(Template.doc_type == "REFINED_DO_LETTER").first()
    if not template:
        print("Template not found!")
        return

    # 3. Generate Doc State (Simulating LLM with extra fields)
    extra_fields = {
        # Sender: Arvind Raghunath Kale
        "SENDER_NAME": "Arvind Raghunath Kale",
        "SENDER_RANK": "Brigadier",
        "SENDER_APPOINTMENT": "Director General Staff Operations",
        "SENDER_ORGANISATION": "Indian Army Headquarters",
        
        # Recipient: Vikram Sen Chauhan
        "RECIPIENT_NAME": "Vikram Sen Chauhan",
        "RECIPIENT_RANK": "Major General",
        "RECIPIENT_SURNAME": "Chauhan",
        "RECIPIENT_DESIGNATION": "Director Personnel Policy",
        "RECIPIENT_ORGANISATION": "Ministry of Defence",
        "RECIPIENT_ADDRESS": "Wing B, Defence Complex, New Delhi",
        
        "DO_NO": "GS/GEN/DO/2026/045",
        "DATE": "13 Feb 2026",
        "SUBJECT": "COORDINATION FOR VIJAY DIWAS PARADE 2026",
        "BODY": (
            "I am writing to initiate coordination for the upcoming Vijay Diwas Parade scheduled for later this month. "
            "Given the significance of the event, I would appreciate a brief meeting next week to align on the "
            "personnel requirements and policy aspects from your directorate."
        )
    }
    
    print("\n--- Generating Doc State ---")
    doc_state = generate_doc_state("REFINED_DO_LETTER", prompt, rules_text, template.zones_json, extra_fields)
    print("Doc State generated.")

    # 4. Render
    out_docx = os.path.abspath("data/demi_official_letter.docx")
    print(f"\n--- Rendering to {out_docx} ---")
    render_docx(template.docx_path, doc_state, out_docx)
    print(f"Rendered successfully to: {out_docx}")
    print(f"File exists check: {os.path.exists(out_docx)}")

    db.close()

if __name__ == "__main__":
    test_rag_gen()
