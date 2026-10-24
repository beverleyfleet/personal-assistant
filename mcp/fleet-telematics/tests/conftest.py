import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "smartcar"
IDS = json.loads((FIX / "ids.json").read_text())
SECRET = "SECRET-client-secret-DO-NOT-LEAK"

FAKE_OP = r'''#!{python}
"""Fake 1Password CLI for tests: `op read [--no-newline] op://V/I/F` and `op item edit I --vault V F[type]=VAL`."""
import json, os, sys
store_path = os.environ["FAKE_OP_STORE"]
store = json.load(open(store_path))
args = sys.argv[1:]
with open(store_path + ".calls", "a") as f:
    f.write(json.dumps([a.split("=", 1)[0] for a in args]) + "\n")   # never log values
if args[:1] == ["read"]:
    ref = [a for a in args[1:] if a.startswith("op://")][0]
    field = ref.rsplit("/", 1)[-1]
    if field not in store:
        sys.stderr.write(f'[ERROR] could not read secret: "{{field}}" isn\'t a field in the item\n'); sys.exit(1)
    sys.stdout.write(store[field]); sys.exit(0)
if args[:2] == ["item", "edit"]:
    assign = args[-1]
    name, value = assign.split("=", 1)
    store[name.split("[", 1)[0]] = value
    json.dump(store, open(store_path, "w")); sys.exit(0)
sys.stderr.write("unsupported fake op call\n"); sys.exit(2)
'''


@pytest.fixture
def fake_op(tmp_path, monkeypatch):
    store = tmp_path / "op_store.json"
    store.write_text(json.dumps({
        "client_id": "test-client-id", "client_secret": SECRET, "application_id": "test-app-id",
        "user_id_ford": IDS["users"]["ford"], "user_id_audi": IDS["users"]["audi"],
        "user_id_chevrolet": IDS["users"]["chevrolet"],
    }))
    op = tmp_path / "bin" / "op"
    op.parent.mkdir()
    op.write_text(FAKE_OP.format(python=sys.executable))
    op.chmod(op.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FLEET_OP_BIN", str(op))
    monkeypatch.setenv("FAKE_OP_STORE", str(store))
    return store


@pytest.fixture
def fleet_env(tmp_path, monkeypatch, fake_op):
    """Temp config with Smartcar ids filled (sprinter left unlinked), temp state dir, replay mode."""
    from fleet_telematics import compat
    from fleet_telematics.config import update_vehicle_fields

    cfg = tmp_path / "fleet.toml"
    shutil.copy(ROOT / "fleet.toml", cfg)
    for key, vid in IDS["vehicles"].items():
        update_vehicle_fields(cfg, key, {"smartcar_vehicle_id": vid})
    state = tmp_path / "state"
    for k, v in {"FLEET_CONFIG": cfg, "FLEET_STATE_DIR": state, "FLEET_SMARTCAR_REPLAY_DIR": FIX,
                 "FLEET_CACHE_SECONDS": "0"}.items():
        monkeypatch.setenv(k, str(v))
    for k in ("FLEET_ALLOW_WRITE", "FLEET_TRIPS_FILE", "FLEET_REDACT_ON_TRIP", "FLEET_AUDIT_LOG", "FLEET_COMPAT_FILE"):
        monkeypatch.delenv(k, raising=False)
    compat._load.cache_clear()
    from fleet_telematics import server
    server._providers.clear()
    yield {"config": cfg, "state": state, "op_store": fake_op}
    server._providers.clear()
