"""1Password CLI (`op`) credential helper.

Recon (2026-10-24) found that expresslanesafety.com/CO has NO user-account login:
every flow is keyed by notice number + plate state + plate, or plate only
("guest pay"). So no tool currently calls this module. It is kept, tested and
documented so that if CTIO adds account logins the server can pick them up
from 1Password without code that ever writes secrets to disk or logs.

Reference: env CTIO_OP_REF, default "op://Beverley Fleet/CTIO ExpressLaneSafety".
Fields read: <ref>/username and <ref>/password.

Auth for `op` itself: either the desktop-app integration on the owner's
machine, or OP_SERVICE_ACCOUNT_TOKEN in the environment (op picks it up
automatically; nothing here reads it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

DEFAULT_OP_REF = "op://Beverley Fleet/CTIO ExpressLaneSafety"


class CredentialError(RuntimeError):
    """Raised with a message that never contains secret material."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:  # belt and braces: never print the secret
        return f"Credentials(username=<redacted>, password=<redacted>)"


def op_ref() -> str:
    return (os.environ.get("CTIO_OP_REF") or DEFAULT_OP_REF).rstrip("/")


def _op_read(ref: str, timeout: float = 30) -> str:
    op = shutil.which(os.environ.get("CTIO_OP_BIN", "op"))
    if not op:
        raise CredentialError("1Password CLI `op` not found on PATH. Install it and sign in, or set OP_SERVICE_ACCOUNT_TOKEN.")
    try:
        proc = subprocess.run([op, "read", "--no-newline", ref], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CredentialError(f"`op read` timed out for field '{ref.rsplit('/', 1)[-1]}' (is 1Password unlocked?)") from None
    if proc.returncode != 0:
        # stderr from op does not contain the secret; trim it anyway.
        raise CredentialError(f"`op read` failed for field '{ref.rsplit('/', 1)[-1]}': {proc.stderr.strip()[:200]}")
    return proc.stdout


def get_credentials() -> Credentials:
    base = op_ref()
    return Credentials(username=_op_read(f"{base}/username"), password=_op_read(f"{base}/password"))


def op_status() -> dict:
    """Non-secret diagnostics: is op installed, which ref would be used."""
    return {
        "op_on_path": bool(shutil.which(os.environ.get("CTIO_OP_BIN", "op"))),
        "op_ref": op_ref(),
        "service_account_token_set": bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")),
        "site_requires_login": False,
    }
