import os
import stat

import pytest

from ctio_mcp import credentials
from ctio_mcp.server import SubmitBlocked, submit_gate
from ctio_mcp.site import format_notice_number, site_plate
from ctio_mcp.vehicles import vehicle_for_plate


@pytest.mark.parametrize("plate,label", [
    ("EIUY09", "2022 Audi Q5"),
    ("eiuy-09", "2022 Audi Q5"),
    ("FNYI83", "2021 Chevrolet Suburban"),
    ("FNYG83", "2023 Ford Transit"),
    ("Temp 8620436", '2024 Ford Transit "Joker"'),
    ("8632431", '2025 Ford Transit "Heaps"'),
])
def test_plate_map(plate, label):
    assert vehicle_for_plate(plate)["label"] == label


def test_unknown_plate():
    assert vehicle_for_plate("ZZZ000") is None


def test_site_formatting():
    assert format_notice_number("901234567879") == "901-234-567-879"
    assert site_plate("Temp 8620436") == "8620436"


def test_submit_gate_default_off(monkeypatch):
    monkeypatch.delenv("CTIO_ALLOW_SUBMIT", raising=False)
    with pytest.raises(SubmitBlocked, match="disabled"):
        submit_gate("x", confirm=True)


def test_submit_gate_needs_confirm(monkeypatch):
    monkeypatch.setenv("CTIO_ALLOW_SUBMIT", "1")
    with pytest.raises(SubmitBlocked, match="confirm=True"):
        submit_gate("x", confirm=False)


def test_submit_gate_never_submits(monkeypatch):
    monkeypatch.setenv("CTIO_ALLOW_SUBMIT", "1")
    with pytest.raises(NotImplementedError, match="stay human"):
        submit_gate("x", confirm=True)


def _fake_op(tmp_path, script):
    p = tmp_path / "op"
    p.write_text("#!/bin/sh\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_op_read_uses_ref_and_redacts(tmp_path, monkeypatch):
    log = tmp_path / "args"
    op = _fake_op(tmp_path, f'echo "$@" >> {log}\ncase "$3" in */username) printf fleetuser;; */password) printf s3cret;; esac\n')
    monkeypatch.setenv("CTIO_OP_BIN", op)
    monkeypatch.setenv("CTIO_OP_REF", "op://Vault X/Item Y")
    c = credentials.get_credentials()
    assert (c.username, c.password) == ("fleetuser", "s3cret")
    assert "s3cret" not in repr(c) and "fleetuser" not in repr(c)
    assert log.read_text().splitlines() == ["read --no-newline op://Vault X/Item Y/username",
                                            "read --no-newline op://Vault X/Item Y/password"]


def test_op_failure_message_has_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("CTIO_OP_BIN", _fake_op(tmp_path, 'echo "[ERROR] item not found" >&2; exit 1\n'))
    with pytest.raises(credentials.CredentialError, match="item not found"):
        credentials.get_credentials()


def test_default_ref(monkeypatch):
    monkeypatch.delenv("CTIO_OP_REF", raising=False)
    assert credentials.op_ref() == "op://Beverley Fleet/CTIO ExpressLaneSafety"
