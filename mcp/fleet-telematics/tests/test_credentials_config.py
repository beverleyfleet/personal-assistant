"""1Password handling, local fallback, config editing, geofence and compat snapshot."""
import json
import os
import stat
import tomllib

import pytest

from fleet_telematics import compat, credentials
from fleet_telematics.config import load_config, update_vehicle_fields
from fleet_telematics.geo import haversine_m, in_geofence

from conftest import ROOT, SECRET


def test_op_read_and_refs(fake_op, monkeypatch):
    assert credentials.ref("client_secret") == "op://Beverley Fleet/Smartcar/client_secret"
    assert credentials.op_read(credentials.ref("client_secret")) == SECRET
    monkeypatch.setenv("FLEET_OP_VAULT", "Other")
    monkeypatch.setenv("FLEET_OP_CLIENT_ID_REF", "op://X/Y/z")
    assert credentials.ref("client_id") == "op://X/Y/z" and credentials.ref("application_id").startswith("op://Other/")


def test_op_missing_field_error_has_no_secret(fake_op):
    with pytest.raises(credentials.SecretError) as ei:
        credentials.op_read("op://Beverley Fleet/Smartcar/nope")
    assert "nope" in str(ei.value) and SECRET not in str(ei.value)


def test_save_user_id_via_op_item_edit(fake_op, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "s"))
    assert credentials.save_user_id("mercedes", "uid-123") == "1password"
    assert json.loads(fake_op.read_text())["user_id_mercedes"] == "uid-123"
    calls = (tmp_path / "op_store.json.calls").read_text()
    assert '"user_id_mercedes[concealed]"' in calls and "uid-123" not in calls
    assert credentials.get_user_id("mercedes") == "uid-123"
    with pytest.raises(credentials.SecretError):
        credentials.save_user_id("../evil", "x")


def test_fallback_file_when_op_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_OP_BIN", str(tmp_path / "no-such-op"))
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "s"))
    where = credentials.save_user_id("ford", "uid-ford")
    assert "WARNING" in capsys.readouterr().err
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
    assert credentials.get_user_id("ford") == "uid-ford"
    with pytest.raises(credentials.SecretError, match="fleet-auth connect audi"):
        credentials.get_user_id("audi")
    with pytest.raises(credentials.SecretError, match="not found"):
        credentials.op_read("op://a/b/c")


def test_update_vehicle_fields_preserves_comments(tmp_path):
    p = tmp_path / "fleet.toml"
    p.write_text((ROOT / "fleet.toml").read_text())
    before = p.read_text()
    update_vehicle_fields(p, "sub", {"smartcar_vehicle_id": "v-sub", "vin": "1GNSKHKD5MR000001"})
    update_vehicle_fields(p, "sprinter", {"smartcar_vehicle_id": "v-spr"})
    after = p.read_text()
    assert "# not on record yet; fleet-auth fills it" in after  # inline comment kept
    assert after.count("\n") == before.count("\n")
    cfg = load_config(p)
    assert cfg.find("sub").vin == "1GNSKHKD5MR000001" and cfg.find("sub").smartcar_vehicle_id == "v-sub"
    assert cfg.find("sprinter").smartcar_vehicle_id == "v-spr" and cfg.find("t24").smartcar_vehicle_id == ""
    tomllib.loads(after)
    with pytest.raises(LookupError):
        update_vehicle_fields(p, "nope", {"vin": "x"})


def test_geofence_matches_osm_parkdia_lot():
    cfg = load_config(ROOT / "fleet.toml")
    g = cfg.geofence
    # OSM way 1131040629 (operator=ParkDIA) bounding box, 2026-10-24
    lat_min, lat_max, lon_min, lon_max = 39.8160338, 39.8200210, -104.6923485, -104.6851164
    assert lat_min < g.lat < lat_max and lon_min < g.lon < lon_max
    for corner in [(lat_min, lon_min), (lat_min, lon_max), (lat_max, lon_min), (lat_max, lon_max)]:
        assert in_geofence(*corner, g.lat, g.lon, g.radius_m)[0], corner
    # the coordinates from the original brief are on E-470, ~3.3 km west: outside
    inside, d = in_geofence(39.8155, -104.7270, g.lat, g.lon, g.radius_m)
    assert not inside and 3000 < d < 3600
    assert abs(haversine_m(39.7392, -104.9903, 39.8180, -104.6888) - 27_200) < 500  # downtown Denver


def test_compat_snapshot_rows():
    cfg = load_config(ROOT / "fleet.toml")
    rows = {v.key: compat.capabilities(v)["matrix_row"] for v in cfg.vehicles}
    assert rows == {"audi": "AUDI-Q5-2018-2026-ICE-US", "sub": "CHEVROLET-Suburban-2015-2026-ICE-US",
                    "t24": "FORD-Transit-2020-2026-ICE-US", "t25": "FORD-Transit-2020-2026-ICE-US",
                    "sprinter": "MERCEDES_BENZ-Sprinter-2017-2026-ICE-US"}
    spr = compat.capabilities(cfg.find("sprinter"))["features"]
    assert spr["lock_command"] and spr["tire_pressure"] and spr["oil_life"] is False
