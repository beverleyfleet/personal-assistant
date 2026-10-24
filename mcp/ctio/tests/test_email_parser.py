from ctio_mcp.email_parser import parse_ctio_email

ANSWER_RECEIVED = """Colorado Transportation Investment Office
Answer Received

LICENSE PLATE: EIUY09
ANSWER NUMBER: 133680

We have received your Answer (dispute). You may pay the balance due $600.00, by Nov 03, 2026
or wait for CTIO to review your Answer.
"""


def test_answer_received_sample():
    r = parse_ctio_email(ANSWER_RECEIVED, subject="Answer Received")
    assert r["status"] == "answer_received"
    assert r["plate"] == "EIUY09"
    assert r["answer_number"] == "133680"
    assert r["amount_due"] == 600.00
    assert r["due_date"] == "2026-11-03"
    assert r["vehicle"] == "2022 Audi Q5"


def test_fields_only_sample_from_brief():
    body = "LICENSE PLATE: EIUY09\nANSWER NUMBER: 133680\nYou may pay the balance due $600.00, by Nov 03, 2026"
    r = parse_ctio_email(body)
    assert (r["plate"], r["answer_number"], r["amount_due"], r["due_date"]) == ("EIUY09", "133680", 600.0, "2026-11-03")


def test_reviewed_with_hearing_deadline_and_temp_plate():
    body = """Dispute (Answer) Reviewed
LICENSE PLATE: TEMP 8620436
ANSWER NUMBER: 140001
CTIO has reviewed your Answer and did not dismiss the civil penalty. You may request a hearing
before an independent Hearing Officer by 12/01/2026. You may pay the balance due $1,250.50, by Dec 15, 2026."""
    r = parse_ctio_email(body)
    assert r["status"] == "answer_reviewed"
    assert r["plate"] == "TEMP 8620436"
    assert r["vehicle"] == '2024 Ford Transit "Joker"'
    assert r["hearing_request_deadline"] == "2026-12-01"
    assert r["amount_due"] == 1250.50
    assert r["due_date"] == "2026-12-15"


def test_dismissed():
    r = parse_ctio_email("Your civil penalty has been dismissed.\nLICENSE PLATE: FNYG83\nANSWER NUMBER: 1", subject="Dismissed")
    assert r["status"] == "dismissed"
    assert r["vehicle"] == "2023 Ford Transit"


def test_notice_with_notice_number():
    body = "Notice of Civil Penalty\nNOTICE NUMBER: 901-234-567-879\nLICENSE PLATE: FNYI83\nAmount Due: $300.00\nDue Date: 11/20/2026"
    r = parse_ctio_email(body)
    assert r["status"] == "notice"
    assert r["notice_number"] == "901-234-567-879"
    assert r["amount_due"] == 300.0
    assert r["due_date"] == "2026-11-20"
    assert r["vehicle"] == "2021 Chevrolet Suburban"


def test_garbage_does_not_raise():
    r = parse_ctio_email("")
    assert r["status"] == "unknown" and r["plate"] is None and r["amount_due"] is None
