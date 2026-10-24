"""fleet-auth connect: Connect URL, localhost redirect capture, user-id storage, vehicle linking."""
import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from fleet_telematics import auth_cli
from fleet_telematics.config import load_config
from fleet_telematics.providers.replay import ReplayTransport
from fleet_telematics.providers.smartcar import SmartcarClient

from conftest import FIX, IDS


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def unlinked_config(fleet_env):
    from conftest import ROOT
    p = fleet_env["config"]
    p.write_text((ROOT / "fleet.toml").read_text())  # pristine: no vehicle ids
    return p


def simulate_browser(params_for_state):
    def on_url(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        cb = urlparse(q["redirect_uri"])
        def go():
            for extra in ({"state": "wrong", "user_id": "attacker"}, params_for_state(q["state"])):
                urllib.request.urlopen(f"http://127.0.0.1:{cb.port}{cb.path}?" + "&".join(f"{k}={v}" for k, v in extra.items()), timeout=5).read()
        threading.Thread(target=go, daemon=True).start()
        on_url.query = q
    return on_url


def test_connect_flow_links_vehicles(fleet_env, capsys):
    cfg_path = unlinked_config(fleet_env)
    port = free_port()
    on_url = simulate_browser(lambda st: {"user_id": IDS["users"]["chevrolet"], "external_id": "beverley-fleet-chevrolet", "state": st})
    args = auth_cli.parser().parse_args(["connect", "chevrolet", "--no-browser", "--port", str(port), "--timeout", "20"])
    client = SmartcarClient(transport=ReplayTransport(FIX))
    assert auth_cli.cmd_connect(args, client=client, on_url=on_url) == 0
    q = on_url.query
    assert q["response_type"] == "none" and q["application_id"] == "test-app-id" and q["make"] == "chevrolet"
    assert q["redirect_uri"] == f"http://localhost:{port}/callback" and q["mode"] == "live"
    assert q["external_id"] == "beverley-fleet-chevrolet" and "control_security" not in q["scope"]
    assert set(q["scope"].split()) >= {"read_odometer", "read_fuel", "read_location", "read_security", "read_vin"}
    # user id saved to (fake) 1Password, never printed in full
    assert json.loads(fleet_env["op_store"].read_text())["user_id_chevrolet"] == IDS["users"]["chevrolet"]
    out = capsys.readouterr()
    assert IDS["users"]["chevrolet"] not in out.out + out.err
    # Suburban had no VIN: matched by make/model/year, VIN filled from the vehicle; other accounts untouched
    cfg = load_config(cfg_path)
    assert cfg.find("sub").smartcar_vehicle_id == IDS["vehicles"]["sub"]
    assert cfg.find("sub").vin == "1GNSKHKD5MR000001"
    assert cfg.find("t24").smartcar_vehicle_id == ""
    assert "linked sub" in out.out


def test_link_by_vin_for_ford(fleet_env, capsys):
    cfg_path = unlinked_config(fleet_env)
    a = auth_cli.parser().parse_args(["link", "ford"])
    assert auth_cli.cmd_link(a, client=SmartcarClient(transport=ReplayTransport(FIX))) == 0
    cfg = load_config(cfg_path)
    assert cfg.find("t24").smartcar_vehicle_id == IDS["vehicles"]["t24"]
    assert cfg.find("t25").smartcar_vehicle_id == IDS["vehicles"]["t25"]
    assert "by vin" in capsys.readouterr().out


def test_connect_error_redirect(fleet_env, capsys):
    unlinked_config(fleet_env)
    port = free_port()
    on_url = simulate_browser(lambda st: {"error": "access_denied", "error_description": "User+denied", "state": st})
    args = auth_cli.parser().parse_args(["connect", "mercedes", "--no-browser", "--port", str(port), "--timeout", "20",
                                         "--allow-write-scope", "--mode", "simulated"])
    assert auth_cli.cmd_connect(args, client=SmartcarClient(transport=ReplayTransport(FIX)), on_url=on_url) == 2
    assert on_url.query["make"] == "mercedes_benz" and "control_security" in on_url.query["scope"]
    assert on_url.query["mode"] == "simulated"
    assert "access_denied" in capsys.readouterr().err


def test_main_reports_missing_application_id(fleet_env, capsys):
    store = json.loads(fleet_env["op_store"].read_text())
    del store["application_id"]
    fleet_env["op_store"].write_text(json.dumps(store))
    assert auth_cli.main(["connect", "ford", "--no-browser", "--timeout", "1"]) == 1
    assert "application_id" in capsys.readouterr().err
