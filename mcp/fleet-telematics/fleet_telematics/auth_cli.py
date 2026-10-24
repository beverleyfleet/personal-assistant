"""fleet-auth: connect brand accounts to Smartcar once, then link vehicles in fleet.toml.

  fleet-auth connect ford          # opens Smartcar Connect, owner logs into FordPass/Ford account
  fleet-auth connect audi          # myAudi
  fleet-auth connect mercedes      # Mercedes me
  fleet-auth connect chevrolet     # myChevrolet
  fleet-auth link ford             # re-match vehicles using the stored user id (no browser)
  fleet-auth link sprinter-acct --assign sprinter=<vehicle_id>
  fleet-auth set-user-id ford      # paste a user id (e.g. from a Dashboard-shared Connect link)
  fleet-auth token-test            # client-credentials token + list connections (counts only)
  fleet-auth status                # what is configured (never prints secret values)
  fleet-auth compat --refresh      # refresh Smartcar's US compatibility matrix for our makes

How it works (Smartcar API v3, docs read 2026-10-24): Connect is launched with response_type=none
and redirect_uri=http://localhost:<port>/callback; Smartcar redirects back with ?user_id=...&state=...
There is no authorization code to exchange and no refresh token: the server later authenticates with
the client-credentials grant and sends the user id as the `sc-user-id` header. So what gets saved
to 1Password is the user id (field user_id_<account>), not a refresh token.
The redirect URI must be registered in Smartcar Dashboard -> Configuration (http is allowed only for localhost).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import queue
import secrets as pysecrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from fleet_telematics import compat, credentials
from fleet_telematics.config import load_config, update_vehicle_fields
from fleet_telematics.providers.smartcar import (CONNECT_URL, DEFAULT_SCOPES, WRITE_SCOPES, SmartcarAPIError,
                                                 SmartcarClient)

ACCOUNT_MAKES = {"ford": "ford", "audi": "audi", "mercedes": "mercedes_benz", "chevrolet": "chevrolet"}


def mask(s: str | None) -> str:
    return "<none>" if not s else (s[:4] + "…" + s[-2:] if len(s) > 8 else "…")


def build_connect_url(application_id: str, redirect_uri: str, state: str, account: str, make: str | None,
                      scopes: list[str], mode: str = "live") -> str:
    q = {"response_type": "none", "application_id": application_id, "redirect_uri": redirect_uri,
         "state": state, "scope": " ".join(scopes), "mode": mode, "external_id": f"beverley-fleet-{account}",
         "flags": "country:US", "approval_prompt": "force"}
    if make:
        q["make"] = make
    return f"{CONNECT_URL}?{urlencode(q)}"


class _Callback(BaseHTTPRequestHandler):
    result: "queue.Queue[dict[str, str]]"

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok = "user_id" in params and "error" not in params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Vehicle account connected. You can close this tab and return to the terminal." if ok
               else "Connection did not complete. Check the terminal for details.")
        self.wfile.write(f"<!doctype html><title>Beverley Fleet</title><p style='font-family:sans-serif'>{msg}</p>".encode())
        self.server.result_q.put(params)  # type: ignore[attr-defined]

    def log_message(self, *a):  # keep the query string (user_id) out of the terminal
        pass


def _serve(port: int) -> tuple[list[HTTPServer], "queue.Queue[dict[str, str]]"]:
    q: "queue.Queue[dict[str, str]]" = queue.Queue()
    servers = []
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        cls = type("S", (HTTPServer,), {"address_family": family})
        try:
            s = cls((host, port), _Callback)
        except OSError:
            continue
        s.result_q = q  # type: ignore[attr-defined]
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)
    if not servers:
        raise SystemExit(f"could not listen on localhost:{port} (in use?). Try --port.")
    return servers, q


def wait_for_redirect(port: int, url: str, expected_state: str, timeout: float = 600,
                      open_browser: bool = True, on_url: Callable[[str], None] | None = None) -> dict[str, str]:
    servers, q = _serve(port)
    try:
        print("Open this URL and log in with the brand account (it does not contain secrets):\n\n  " + url + "\n",
              file=sys.stderr)
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        while True:
            try:
                params = q.get(timeout=timeout)
            except queue.Empty:
                raise SystemExit(f"timed out after {timeout:.0f}s waiting for the Smartcar redirect") from None
            if params.get("state") != expected_state:
                print("ignoring a callback with a wrong/missing state parameter", file=sys.stderr)
                continue
            return params
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


async def link_vehicles(account: str, user_id: str, client: SmartcarClient,
                        assign: dict[str, str] | None = None, config_path=None) -> list[dict[str, Any]]:
    """Match the user's Smartcar connections to fleet.toml vehicles of this account and write ids/VINs."""
    cfg = load_config(config_path)
    mine = [v for v in cfg.vehicles if v.smartcar_account == account and v.provider == "smartcar"]
    report = []
    for key, vid in (assign or {}).items():
        v = cfg.find(key)
        update_vehicle_fields(cfg.path, v.key, {"smartcar_vehicle_id": vid})
        report.append({"vehicle": v.key, "smartcar_vehicle_id": vid, "matched_by": "--assign"})
    conns = await client.list_connections(user_id=user_id)
    taken = {r["vehicle"] for r in report}
    for c in conns:
        vid = c["relationships"]["vehicle"]["data"]["id"]
        attrs = c["attributes"]["vehicle"]
        vin = None
        try:
            one = await client.request("GET", f"/vehicles/{vid}/signals/vehicleidentification-vin", user_id=user_id)
            body = one["data"]["attributes"]
            if body.get("status", {}).get("value") == "SUCCESS":
                vin = (body.get("body") or {}).get("value")
        except SmartcarAPIError:
            pass
        target, how = None, None
        if vin:
            target = next((v for v in mine if v.vin and v.vin.upper() == vin.upper()), None)
            how = "vin" if target else None
        if target is None:
            cands = [v for v in mine if v.key not in taken and not v.vin and v.make.lower() == str(attrs.get("make", "")).lower()
                     and v.model.lower() in str(attrs.get("model", "")).lower() and int(attrs.get("year", 0)) == v.year]
            if len(cands) == 1:
                target, how = cands[0], "make/model/year"
        status = (c["attributes"].get("status") or {}).get("name")
        if target is None:
            report.append({"vehicle": None, "smartcar_vehicle_id": vid, "vin": vin, "status": status,
                           "seen": f"{attrs.get('year')} {attrs.get('make')} {attrs.get('model')} ({attrs.get('mode')})",
                           "matched_by": None})
            continue
        updates = {"smartcar_vehicle_id": vid}
        if vin and not target.vin:
            updates["vin"] = vin
        update_vehicle_fields(cfg.path, target.key, updates)
        taken.add(target.key)
        report.append({"vehicle": target.key, "smartcar_vehicle_id": vid, "vin": vin, "status": status, "matched_by": how})
    return report


def _print_report(report: list[dict[str, Any]]) -> None:
    if not report:
        print("No connections found for this user id.")
    for r in report:
        if r["vehicle"]:
            print(f"  linked {r['vehicle']:<9} -> {r['smartcar_vehicle_id']} (by {r['matched_by']}, status {r.get('status')})")
        else:
            print(f"  UNMATCHED {r['seen']} vehicle_id={r['smartcar_vehicle_id']} vin={r.get('vin')}; "
                  f"use: fleet-auth link <account> --assign <key>={r['smartcar_vehicle_id']}")


def cmd_connect(a: argparse.Namespace, client: SmartcarClient | None = None,
                on_url: Callable[[str], None] | None = None) -> int:
    account = a.account.lower()
    make = a.make or ACCOUNT_MAKES.get(account)
    app_id = credentials.op_read(credentials.ref("application_id"))
    scopes = list(a.scope or DEFAULT_SCOPES) + (list(WRITE_SCOPES) if a.allow_write_scope else [])
    state = pysecrets.token_urlsafe(24)
    redirect = f"http://localhost:{a.port}/callback"
    url = build_connect_url(app_id, redirect, state, account, make, scopes, a.mode)
    params = wait_for_redirect(a.port, url, state, timeout=a.timeout, open_browser=not a.no_browser, on_url=on_url)
    if "error" in params:
        print(f"Connect returned an error: {params.get('error')}: {params.get('error_description', '')}", file=sys.stderr)
        extra = {k: params[k] for k in ("make", "model", "year", "vin") if k in params}
        if extra:
            print(f"  vehicle: {extra}", file=sys.stderr)
        return 2
    user_id = params.get("user_id") or params.get("userId")
    if not user_id:
        print("Redirect had no user_id; look the connection up with: fleet-auth link "
              f"{account} (uses external_id beverley-fleet-{account})", file=sys.stderr)
        return 2
    where = credentials.save_user_id(account, user_id)
    print(f"Saved Smartcar user id {mask(user_id)} for '{account}' to {where}.")
    client = client or SmartcarClient()
    report = asyncio.run(_link_and_close(account, user_id, client, None))
    _print_report(report)
    return 0


async def _link_and_close(account, user_id, client, assign):
    try:
        return await link_vehicles(account, user_id, client, assign)
    finally:
        await client.aclose()


def cmd_link(a: argparse.Namespace, client: SmartcarClient | None = None) -> int:
    account = a.account.lower()
    client = client or SmartcarClient()
    try:
        user_id = credentials.get_user_id(account)
    except credentials.SecretError:
        async def by_external():
            conns = await client.list_connections(external_id=f"beverley-fleet-{account}")
            return conns[0]["attributes"]["user"]["id"] if conns else None
        user_id = asyncio.run(by_external())
        if not user_id:
            print(f"No user id stored and no connection with external_id beverley-fleet-{account}.", file=sys.stderr)
            return 2
        credentials.save_user_id(account, user_id)
    assign = dict(x.split("=", 1) for x in (a.assign or []))
    _print_report(asyncio.run(_link_and_close(account, user_id, client, assign)))
    return 0


def cmd_set_user_id(a: argparse.Namespace) -> int:
    uid = getpass.getpass(f"Smartcar user id for '{a.account}' (input hidden): ").strip()
    if not uid:
        return 2
    print(f"Saved to {credentials.save_user_id(a.account.lower(), uid)}.")
    return 0


def cmd_token_test(a: argparse.Namespace, client: SmartcarClient | None = None) -> int:
    client = client or SmartcarClient()

    async def run():
        try:
            await client.token()
            conns = await client.list_connections()
            return len(conns)
        finally:
            await client.aclose()
    n = asyncio.run(run())
    print(f"Token OK (not shown). Application has {n} vehicle connection(s).")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    cfg = load_config()
    st = credentials.status()
    print(f"config: {cfg.path}")
    for k, v in st.items():
        print(f"  {k}: {v}")
    for v in cfg.vehicles:
        print(f"  {v.key:<9} {v.name:<30} provider={v.provider:<12} account={v.smartcar_account or '-':<10} "
              f"linked={'yes' if v.smartcar_vehicle_id else 'no'}")
    return 0


def cmd_compat(a: argparse.Namespace) -> int:
    if a.refresh:
        print(f"refreshed -> {compat.refresh()}")
    for v in load_config().vehicles:
        c = compat.capabilities(v)
        yes = [k for k, ok in c["features"].items() if ok]
        no = [k for k, ok in c["features"].items() if ok is False]
        print(f"{v.key:<9} {c['matrix_row']}\n   yes: {', '.join(yes) or '-'}\n   no:  {', '.join(no) or '-'}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet-auth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect", help="run Smartcar Connect for one brand account")
    c.add_argument("account", help="ford | audi | mercedes | chevrolet (or any name with --make)")
    c.add_argument("--make", help="Smartcar make code to skip the brand picker (e.g. mercedes_benz)")
    c.add_argument("--mode", choices=["live", "simulated"], default="live")
    c.add_argument("--port", type=int, default=8000)
    c.add_argument("--timeout", type=float, default=600)
    c.add_argument("--no-browser", action="store_true")
    c.add_argument("--allow-write-scope", action="store_true", help="also request control_security (lock/unlock)")
    c.add_argument("--scope", nargs="+", help="override permissions (default: read-only set)")
    l_ = sub.add_parser("link", help="match connections to fleet.toml using the stored user id")
    l_.add_argument("account")
    l_.add_argument("--assign", nargs="+", metavar="KEY=VEHICLE_ID")
    s = sub.add_parser("set-user-id", help="store a user id by hand (hidden input)")
    s.add_argument("account")
    sub.add_parser("token-test", help="verify client credentials")
    sub.add_parser("status", help="show configuration (no secret values)")
    cp = sub.add_parser("compat", help="show Smartcar US compatibility for the fleet")
    cp.add_argument("--refresh", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    fn = {"connect": cmd_connect, "link": cmd_link, "set-user-id": cmd_set_user_id, "token-test": cmd_token_test,
          "status": cmd_status, "compat": cmd_compat}[a.cmd]
    try:
        return fn(a)
    except (credentials.SecretError, SmartcarAPIError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
