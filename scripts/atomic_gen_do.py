import os
import re
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import Template
from app.services.rag import search_rules
from app.services.render import render_docx
from app.providers.llm_provider import generate_doc_state

def fix_and_gen():
    db: Session = SessionLocal()
    
    # 0. Fix DB entry
    t_do = db.query(Template).filter(Template.name.like('%Refined_DO_Template%')).first()
    if t_do:
        t_do.doc_type = 'REFINED_DO_LETTER'
        db.commit()
        print(f"Fixed Template ID {t_do.id} to REFINED_DO_LETTER")
    else:
        print("CRITICAL: Refined_DO_Template not found in DB at all!")
        # List what is there
        [print(f"  Existing: {t.name} ({t.doc_type})") for t in db.query(Template).all()]
        return

    # 1. RAG Query
    prompt = "write a demi official letter to Major General Vikram Sen Chauhan regarding coordination for the upcoming Vijay Diwas Parade"
    print(f"Prompt: {prompt}")
    
    chunks = search_rules(db, prompt, "REFINED_DO_LETTER", k=3)
    rules_text = "\n\n".join([c.text for c in chunks])
    
    # 2. Identify Template
    template = db.query(Template).filter(Template.doc_type == "REFINED_DO_LETTER").first()
    if not template:
        print("Template not found after fix!")
        return

    # 3. Generate Doc State
    extra_fields = {
        # Sender: Arvind Raghunath Kale
        "SENDER_NAME": "Arvind Raghunath Kale",
        "SENDER_RANK": "Brigadier",
        "SENDER_APPOINTMENT": "Director General Staff Operations",
        "SENDER_ORGANISATION": "Indian Army Headquarters",
        "TELE": "011-23010000",
        "FAX": "011-23011111",
        "EMAIL": "arkale.army@nic.in",
        "SENDER_ADDR_1": "South Block, Ministry of Defence",
        "SENDER_ADDR_2": "New Delhi - 110011",
        
        # Recipient: Vikram Sen Chauhan
        "RECIPIENT_NAME": "Vikram Sen Chauhan",
        "RECIPIENT_RANK": "Major General",
        "RECIPIENT_SURNAME": "Chauhan",
        "RECIPIENT_DESIGNATION": "Director Personnel Policy",
        "RECIPIENT_ORGANISATION": "Ministry of Defence",
        "RECIPIENT_ADDRESS": "Wing B, Defence Complex, New Delhi",
        
        "DO_NO": "GS/GEN/DO/2026/045",
        "DATE": "13 Feb 2026",
        "SUBJECT_UPPER": "COORDINATION FOR VIJAY DIWAS PARADE – PERSONNEL REQUIREMENTS & POLICY ASPECTS",
        "BODY": (
            "I am writing to initiate coordination for the upcoming Vijay Diwas Parade scheduled for later this month. "
            "Given the significance of the event, I would appreciate a brief meeting next week to align on the "
            "personnel requirements and policy aspects from your directorate."
        )
    }
    
    doc_state = generate_doc_state("REFINED_DO_LETTER", prompt, rules_text, template.zones_json, extra_fields)
    
    # 4. Render
    out_docx = os.path.abspath("data/demi_official_letter_v6.docx")
    render_docx(template.docx_path, doc_state, out_docx)
    print(f"Rendered successfully to: {out_docx}")
    
    db.close()

if __name__ == "__main__":
    fix_and_gen()
