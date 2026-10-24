import os
from datetime import datetime
from pathlib import Path

import anyio
import pymupdf
import pytest

from ctio_mcp import packet, rental, site
from ctio_mcp.rental import Address, Penalty, RentalDeclarationError, select_and_validate
from ctio_mcp.server import SubmitBlocked, submit_gate_rental

FIXTURE = Path(__file__).parent / "fixtures" / "dispute_rental.html"
ADDR = Address(street="1234 Larimer St", apt="Unit 5", city="Denver", state="CO", zip="80205")


# ------------------------------------------------------------ date parsing / range validation
@pytest.mark.parametrize("raw,expected", [
    ("10/12/2026 3:45:12 PM", datetime(2026, 10, 12, 15, 45, 12)),
    ("2026-10-12T15:45:12.123-06:00", datetime(2026, 10, 12, 15, 45, 12)),
    ("10/12/2026", datetime(2026, 10, 12)),
    ("Oct 12, 2026 3:45 PM", datetime(2026, 10, 12, 15, 45)),
])
def test_parse_when(raw, expected):
    assert rental.parse_when(raw) == expected


def test_rental_bounds_whole_days():
    assert rental.parse_rental_bound("2026-10-10", end=False) == datetime(2026, 10, 10, 0, 0)
    assert rental.parse_rental_bound("2026-10-21", end=True) == datetime(2026, 10, 21, 23, 59, 59)
    assert rental.parse_rental_bound("2026-10-21T09:30", end=True) == datetime(2026, 10, 21, 9, 30)


def _p(num, when, disputable=True):
    return Penalty(number=num, when=rental.parse_when(when), when_raw=when, location="I-25", disputable=disputable)


START, END = datetime(2026, 10, 10), datetime(2026, 10, 21, 23, 59, 59)


def test_validate_inside_range_all_disputable():
    ps = [_p("123456789", "10/12/2026 3:45 PM"), _p("987654321", "10/21/2026 11:00 PM"), _p("555", "09/01/2026", False)]
    chosen = select_and_validate(ps, None, START, END)
    assert [p.number for p in chosen] == ["123456789", "987654321"]


def test_validate_refuses_outside_range():
    ps = [_p("123456789", "10/22/2026 12:00:01 AM")]
    with pytest.raises(RentalDeclarationError, match="OUTSIDE rental"):
        select_and_validate(ps, None, START, END)


def test_validate_refuses_outside_with_time_bounds():
    end = rental.parse_rental_bound("2026-10-12T15:00", end=True)
    with pytest.raises(RentalDeclarationError, match="OUTSIDE"):
        select_and_validate([_p("1", "10/12/2026 3:45 PM")], None, START, end)


def test_validate_requested_numbers_and_errors():
    ps = [_p("123456789", "10/12/2026 3:45 PM"), _p("555000111", "09/01/2026", False)]
    assert [p.number for p in select_and_validate(ps, ["123 456 789"], START, END)] == ["123456789"]
    with pytest.raises(RentalDeclarationError, match="not on this notice"):
        select_and_validate(ps, ["999999999"], START, END)
    with pytest.raises(RentalDeclarationError, match="not disputable"):
        select_and_validate(ps, ["555000111"], START, END)
    with pytest.raises(RentalDeclarationError, match="unreadable"):
        select_and_validate([Penalty(number="1", when=None, when_raw="garbage")], None, START, END)


def test_penalties_from_json_merges_sources():
    viol = {"Violations": [{"TVNID": 7, "NoticeNumber": "901-234-567-879"}],
            "Details": [{"TVNID": 7, "CivielPenalityNumber": "123456789", "DataTime": "10/12/2026 3:45 PM", "Location": "I-25 NB"}]}
    disp = {"Details": [{"TVNID": 7, "TrxnNum": 123456789, "TransactionDate": "2026-10-12T15:45:00", "Lane": "I-25 NB MP 210",
                         "Disputable": 1, "StatusCode": "UNPAID", "TollAmount": "300.00"}]}
    [p] = rental.penalties_from_dispute_json(disp, viol)
    assert p.when == datetime(2026, 10, 12, 15, 45) and p.disputable is True and p.notice_number == "901-234-567-879"


# ------------------------------------------------------------ form values / attachments
def _values(**kw):
    args = dict(renter_name="Jordan A Smith", renter_address=ADDR, rental_start="2026-10-10", rental_end="2026-10-21",
                contact_email="owner@example.com", contact_phone="(303) 555-0142", details=None, plate="EIUY09")
    args.update(kw)
    return rental.build_form_values(**args)


def test_build_form_values():
    v = _values()["values"]
    assert v["first_name"] == "Jordan A" and v["last_name"] == "Smith" and v["state"] == "Colorado"
    assert v["contact_phone"] == "3035550142" and v["start_date"] == "2026-10-10"
    assert "EIUY09" in v["details"] and len(v["details"]) <= 500


@pytest.mark.parametrize("kw,msg", [
    ({"renter_name": "Cher"}, "first and last"),
    ({"rental_end": "2026-10-01"}, "before rental_start"),
    ({"contact_email": ""}, "contact_email"),
    ({"contact_phone": "555"}, "10-digit"),
    ({"renter_address": Address(street="x", city="Denver", state="ZZ", zip="1")}, "Unknown renter state"),
])
def test_build_form_values_errors(kw, msg):
    with pytest.raises(RentalDeclarationError, match=msg):
        _values(**kw)


def test_check_attachment(tmp_path):
    ok = tmp_path / "agreement.pdf"; ok.write_bytes(b"%PDF-1.4\n")
    assert rental.check_attachment(ok) == ok
    upper = tmp_path / "agreement.PDF"; upper.write_bytes(b"x")
    with pytest.raises(RentalDeclarationError, match="not accepted"):
        rental.check_attachment(upper)
    big = tmp_path / "big.pdf"; big.write_bytes(b"0" * (5 * 1024 * 1024 + 1))
    with pytest.raises(RentalDeclarationError, match="5 MB"):
        rental.check_attachment(big)


# ------------------------------------------------------------ gating
def test_rental_gate(monkeypatch):
    monkeypatch.delenv("CTIO_ALLOW_SUBMIT", raising=False)
    with pytest.raises(SubmitBlocked, match="not set"):
        submit_gate_rental(True)
    monkeypatch.setenv("CTIO_ALLOW_SUBMIT", "1")
    with pytest.raises(SubmitBlocked, match="confirm=True"):
        submit_gate_rental(False)
    submit_gate_rental(True)  # passes


# ------------------------------------------------------------ packet
def _agreement_pdf(path):
    d = pymupdf.open(); pg = d.new_page(); pg.insert_text((72, 72), "RENTAL AGREEMENT #RA-1001 Jordan A Smith"); d.save(path); d.close()


def test_packet_from_pdf(tmp_path):
    ag = tmp_path / "ra.pdf"; _agreement_pdf(ag)
    r = packet.build_packet(out_path=tmp_path / "out" / "p.pdf", agreement_path=ag, plate="EIUY09",
                            notice_number="901-234-567-879", penalty_numbers=["123456789"], renter_name="Jordan A Smith",
                            renter_address=ADDR, rental_start="2026-10-10", rental_end="2026-10-21",
                            penalties_detail=[{"civil_penalty_number": "123456789", "date_time": "2026-10-12T15:45:12", "location": "I-25 NB"}])
    assert r["pages"] == 2 and r["within_ctio_5mb_limit"]
    with pymupdf.open(r["packet_path"]) as d:
        cover = d[0].get_text()
        assert "Beverley Fleet" in cover and "EIUY09" in cover and "2022 Audi Q5" in cover
        assert "901-234-567-879" in cover and "123 456 789" in cover and "Jordan A Smith" in cover
        assert "1234 Larimer St" in cover and "Signature" in cover and "2026-10-10" in cover
        assert "RA-1001" in d[1].get_text()


def test_packet_from_png(tmp_path):
    png = tmp_path / "ra.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), False); pix.clear_with(200); pix.save(png)
    r = packet.build_packet(out_path=tmp_path / "p.pdf", agreement_path=png, plate="Temp 8620436", notice_number="1",
                            penalty_numbers=[], renter_name="A B", renter_address=ADDR, rental_start="a", rental_end="b")
    assert r["pages"] == 2


# ------------------------------------------------------------ fixture end-to-end (local HTML, no network)
@pytest.fixture
def fixture_site(monkeypatch, tmp_path):
    async def fake_login(page, captured, notice_number, plate, state):
        await page.goto(FIXTURE.as_uri())
        return {}
    monkeypatch.setattr(site, "_notice_login", fake_login)
    monkeypatch.setenv("CTIO_ARTIFACT_DIR", str(tmp_path / "shots"))
    ag = tmp_path / "agreement.pdf"; _agreement_pdf(ag)
    return ag


def _run(ag, submit=False, **kw):
    args = dict(notice_number="901234567879", plate="EIUY09", state="CO", renter_name="Jordan A Smith",
                renter_address=ADDR, rental_start="2026-10-10", rental_end="2026-10-21", agreement_path=str(ag),
                civil_penalty_numbers=None, contact_email="owner@example.com", contact_phone="3035550142",
                details=None, submit=submit)
    args.update(kw)
    return anyio.run(lambda: site.rental_declaration(**args))


def test_fixture_prepare_fills_and_stops(fixture_site):
    r = _run(fixture_site)
    assert [p["civil_penalty_number"] for p in r["penalties_covered"]] == ["123456789", "987654321"]
    assert r["penalties_covered"][0]["location"] == "I-25 Express NB MP 210"
    assert r["mismatches"] == {}
    rb = r["fields_read_back"]
    assert rb["reason"] == "Rental" and rb["state"] == "Colorado" and rb["start_date"] == "2026-10-10"
    assert rb["agree_ecomm_checked"] is True and rb["perjury_checked"] is False
    assert r["attachment"]["uploaded_to_ctio"] is True and "stopped_before" in r
    assert all(os.path.exists(s) for s in r["screenshots"])


def test_fixture_refuses_out_of_range(fixture_site):
    with pytest.raises(site.CtioError, match="OUTSIDE rental"):
        _run(fixture_site, rental_start="2026-10-15", rental_end="2026-10-25")


def test_fixture_selected_penalty_only(fixture_site):
    r = _run(fixture_site, rental_start="2026-10-15", rental_end="2026-10-25", civil_penalty_numbers=["987 654 321"])
    assert r["penalties_ticked"] == ["987654321"]


def test_fixture_submit_clicks_and_captures(fixture_site, monkeypatch):
    # Exercise the submit path of the browser flow against the LOCAL fixture only.
    captured_payload = {}
    orig = site._screenshot

    async def shot_and_grab(page, tag):
        if tag.startswith("rental-submitted"):
            captured_payload.update(await page.evaluate("window.__ticket") or {})
        return await orig(page, tag)
    monkeypatch.setattr(site, "_screenshot", shot_and_grab)
    r = _run(fixture_site, submit=True)
    assert r["submitted"] and r["acknowledged"]
    assert captured_payload["ViolationNumbers"] == "123456789,987654321"
    f = captured_payload["Fields"]
    assert (f["FirstName"], f["LastName"], f["City"], f["State"], f["ZipCode"]) == ("Jordan A", "Smith", "Denver", "Colorado", "80205")
    assert captured_payload["Phone"] == "(303) 555-0142" and captured_payload["Attachments"] == ["agreement.pdf"]


# ------------------------------------------------------------ live: selectors still present in the deployed bundle
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CTIO_LIVE") != "1", reason="set CTIO_LIVE=1")
def test_live_bundle_still_has_rental_selectors():
    import re
    import urllib.request

    def get(url):
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    base = site.BASE_URL + "/"
    html = get(base)
    runtime = re.search(r'src="(runtime\.[0-9a-f]+\.js)"', html).group(1)
    rt = get(base + runtime)
    suffix = re.search(r'\.u=e=>e\+"(\.[0-9a-f]+\.js)"', rt).group(1)
    chunk = get(base + "216" + suffix)
    needles = ['"id","DisCivPenReasonSelect"', '"Rental"', '"Please Enter Email"', '"Please Enter Phone"',
               "Please Enter Rental Agreement Start Date", "Please Enter Rental Agreement End Date",
               "Please Enter First Name", "Please Enter Last Name", "Please Enter Mailing Address",
               "Please Enter Apt / Suite", "Please Enter City", "Please select State", "Please Enter ZipCode",
               '"accept",".pdf,.jpg,.jpeg,.png","type","file","id","file"', '"id","declaration-agree"',
               '"id","declaration-submit"', '"id","RentalSubmitId"', '"aria-label","Please Check","type","checkbox"',
               "Click here to Show Civil Penalties", "expanded-table", '"id","disputeLinkId"', "Dispute Acknowledged",
               '[".pdf",".png",".jpeg",".jpg"]', "Maximum file size is 5mb", 'value:"Colorado"']
    missing = [n for n in needles if n not in chunk]
    assert not missing, f"bundle 216{suffix} changed; missing: {missing}"
