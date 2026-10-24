"""Secrets via the 1Password CLI (`op`). Values are never logged, printed or returned to MCP clients.

1Password item (default vault "Beverley Fleet", item "Smartcar"), fields:
  client_id         Smartcar API credential client id      (Dashboard -> API Credentials)
  client_secret     Smartcar API credential client secret  (shown once at creation)
  application_id    Smartcar application id for the Connect URL (Dashboard -> Configuration)
  user_id_<account> Smartcar user id per connected brand login, written by `fleet-auth`
                    (e.g. user_id_ford, user_id_audi, user_id_mercedes, user_id_chevrolet)

Smartcar API v3 has no per-vehicle refresh tokens (see README, "Auth model"): the long-lived
things to store are the client secret and one user id per brand account.

Env overrides (all optional):
  FLEET_OP_VAULT / FLEET_OP_ITEM            default "Beverley Fleet" / "Smartcar"
  FLEET_OP_CLIENT_ID_REF, FLEET_OP_CLIENT_SECRET_REF, FLEET_OP_APPLICATION_ID_REF
                                            full op:// refs, override the vault/item defaults
  FLEET_OP_BIN                              path to `op` (default: found on PATH)
If `op` is unavailable, user ids fall back to a 0600 JSON file in the state dir (with a warning);
the client secret is never written to disk by this code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fleet_telematics.config import state_dir

log = logging.getLogger(__name__)

ACCOUNT_RE = re.compile(r"^[a-z0-9_]{1,40}$")


class SecretError(RuntimeError):
    """Raised with a message that never contains secret material."""


def vault() -> str:
    return os.environ.get("FLEET_OP_VAULT") or "Beverley Fleet"


def item() -> str:
    return os.environ.get("FLEET_OP_ITEM") or "Smartcar"


def ref(field: str) -> str:
    env = {"client_id": "FLEET_OP_CLIENT_ID_REF", "client_secret": "FLEET_OP_CLIENT_SECRET_REF",
           "application_id": "FLEET_OP_APPLICATION_ID_REF"}.get(field)
    if env and os.environ.get(env):
        return os.environ[env]
    return f"op://{vault()}/{item()}/{field}"


def op_bin() -> str | None:
    return shutil.which(os.environ.get("FLEET_OP_BIN", "op"))


def _field_name(r: str) -> str:
    return r.rstrip("/").rsplit("/", 1)[-1]


def op_read(reference: str, timeout: float = 30) -> str:
    op = op_bin()
    if not op:
        raise SecretError("1Password CLI `op` not found. Install it and sign in (or set OP_SERVICE_ACCOUNT_TOKEN).")
    try:
        p = subprocess.run([op, "read", "--no-newline", reference], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SecretError(f"`op read` timed out for field '{_field_name(reference)}' (is 1Password unlocked?)") from None
    if p.returncode != 0:
        raise SecretError(f"`op read` failed for field '{_field_name(reference)}': {p.stderr.strip()[:200]}")
    if not p.stdout:
        raise SecretError(f"field '{_field_name(reference)}' is empty in 1Password")
    return p.stdout


def op_set_field(field: str, value: str, timeout: float = 60) -> None:
    """`op item edit <item> --vault <vault> '<field>[concealed]=<value>'`. Output is discarded.

    Note: the value is passed as a command-line argument (op's documented assignment syntax),
    so it is briefly visible to other processes of the same user. A Smartcar user id alone does
    not grant access without the client secret; the client secret is never written by this code.
    """
    op = op_bin()
    if not op:
        raise SecretError("1Password CLI `op` not found")
    if not ACCOUNT_RE.match(field.removeprefix("user_id_")):
        raise SecretError(f"refusing to write unexpected field name {field!r}")
    p = subprocess.run([op, "item", "edit", item(), "--vault", vault(), f"{field}[concealed]={value}"],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise SecretError(f"`op item edit` failed for field '{field}': {p.stderr.strip()[:200]}")


# ---- user ids (sc-user-id) per brand account ------------------------------------------------

def _fallback_file() -> Path:
    return state_dir() / "smartcar-user-ids.json"


def _read_fallback() -> dict[str, str]:
    f = _fallback_file()
    if not f.exists():
        return {}
    if f.stat().st_mode & 0o077:
        log.warning("%s is readable by group/others; fixing to 0600", f)
        os.chmod(f, 0o600)
    return json.loads(f.read_text() or "{}")


def _write_fallback(data: dict[str, str]) -> Path:
    f = _fallback_file()
    fd = os.open(f.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(f.with_suffix(".tmp"), f)
    os.chmod(f, 0o600)
    return f


def save_user_id(account: str, user_id: str) -> str:
    """Store a Smartcar user id. Returns where it went ("1password" or the file path)."""
    if not ACCOUNT_RE.match(account):
        raise SecretError(f"bad account name {account!r} (use lowercase letters, digits, _)")
    if op_bin():
        op_set_field(f"user_id_{account}", user_id)
        return "1password"
    print(f"WARNING: 1Password CLI `op` not found; saving the Smartcar user id for '{account}' to a local "
          f"0600 file instead ({_fallback_file()}). Move it into 1Password when you can.", file=sys.stderr)
    data = _read_fallback()
    data[account] = user_id
    return str(_write_fallback(data))


def get_user_id(account: str) -> str:
    if op_bin():
        try:
            return op_read(f"op://{vault()}/{item()}/user_id_{account}")
        except SecretError as e:
            fb = _read_fallback().get(account)
            if fb:
                return fb
            raise SecretError(f"no Smartcar user id for account '{account}'. Run: fleet-auth connect {account}. ({e})") from None
    fb = _read_fallback().get(account)
    if not fb:
        raise SecretError(f"no Smartcar user id for account '{account}' (op unavailable and no local file). "
                          f"Run: fleet-auth connect {account}")
    return fb


def status() -> dict:
    """Non-secret diagnostics."""
    return {
        "op_on_path": bool(op_bin()),
        "op_vault": vault(),
        "op_item": item(),
        "client_id_ref": ref("client_id"),
        "client_secret_ref": ref("client_secret"),
        "application_id_ref": ref("application_id"),
        "service_account_token_set": bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")),
        "fallback_user_id_file": str(_fallback_file()) if _fallback_file().exists() else None,
    }
