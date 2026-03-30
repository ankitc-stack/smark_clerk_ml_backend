def classify_doc_type(prompt: str, hint: str | None = None) -> str:
    if hint:
        return hint
    p = prompt.upper()
    if "SIGNAL" in p or "DTG" in p or "STOP" in p:
        return "SIGNAL_FORM"
    if "MOVEMENT ORDER" in p or "DISTR" in p:
        return "MOVEMENT_ORDER"
    if "LEAVE CERTIFICATE" in p or "LEAVE" in p:
        return "LEAVE_CERTIFICATE"
    if "GOVERNMENT OF INDIA" in p or "MINISTRY" in p:
        return "GOI_LETTER"
    return "GENERAL_LETTER"
