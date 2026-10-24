"""Start the server as a subprocess over stdio and talk MCP to it."""
import os
import sys

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

EXPECTED = {
    "ctio_lookup_notice", "ctio_list_notices", "ctio_parse_email", "ctio_vehicle_for_plate",
    "ctio_prepare_payment", "ctio_prepare_hearing_request", "ctio_status",
    "ctio_prepare_rental_declaration", "ctio_submit_rental_declaration", "ctio_build_declaration_packet",
}
RENTAL_ARGS = {"notice_number": "901234567879", "plate": "EIUY09", "renter_name": "Jordan Smith",
               "renter_address": {"street": "1 Main St", "city": "Denver", "state": "CO", "zip": "80205"},
               "rental_start": "2026-10-10", "rental_end": "2026-10-21", "agreement_path": "/nonexistent.pdf"}


def _params(**env):
    return StdioServerParameters(command=sys.executable, args=["-m", "ctio_mcp"], env={**os.environ, **env})


def _text(result):
    return " ".join(getattr(c, "text", "") for c in result.content)


def test_stdio_list_and_call_tools():
    async def run():
        async with Client(_params(CTIO_ALLOW_SUBMIT="")) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert EXPECTED <= names, names

            r = await client.call_tool("ctio_parse_email", {"text": "LICENSE PLATE: EIUY09\nANSWER NUMBER: 133680\nYou may pay the balance due $600.00, by Nov 03, 2026"})
            assert not r.is_error
            sc = r.structured_content
            assert sc["plate"] == "EIUY09" and sc["answer_number"] == "133680" and sc["due_date"] == "2026-11-03"

            r = await client.call_tool("ctio_vehicle_for_plate", {"plate": "Temp 8632431"})
            assert r.structured_content["nickname"] == "Heaps"

            # Gate: submit refused without env flag, before any browser is launched.
            r = await client.call_tool("ctio_prepare_payment", {"plate": "EIUY09", "submit": True, "confirm": True})
            assert r.is_error and "disabled" in _text(r)

            # Rental submit gate: env off -> refused before any browser/network.
            r = await client.call_tool("ctio_submit_rental_declaration", {**RENTAL_ARGS, "confirm": True})
            assert r.is_error and "Nothing was sent" in _text(r), _text(r)
            # Prepare validates locally first (missing file) without touching the site.
            r = await client.call_tool("ctio_prepare_rental_declaration", RENTAL_ARGS)
            assert r.is_error and "agreement_path not found" in _text(r), _text(r)
            tools_by_name = {t.name: t for t in tools.tools}
            assert tools_by_name["ctio_submit_rental_declaration"].annotations.destructive_hint is True
        return names

    names = anyio.run(run)
    print("tools:", sorted(names))


def test_stdio_gate_with_env_still_not_implemented():
    async def run():
        async with Client(_params(CTIO_ALLOW_SUBMIT="1")) as client:
            r = await client.call_tool("ctio_prepare_hearing_request", {"notice_number": "1", "plate": "X", "submit": True})
            assert r.is_error and "confirm=True" in _text(r)
            r = await client.call_tool("ctio_prepare_hearing_request", {"notice_number": "1", "plate": "X", "submit": True, "confirm": True})
            assert r.is_error and "not implemented" in _text(r)
            # Env on but confirm missing -> rental submit still refused.
            r = await client.call_tool("ctio_submit_rental_declaration", RENTAL_ARGS)
            assert r.is_error and "confirm=True" in _text(r)

    anyio.run(run)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CTIO_LIVE") != "1", reason="set CTIO_LIVE=1 to hit expresslanesafety.com")
def test_live_guest_list_and_bad_notice():
    plate = os.environ.get("CTIO_LIVE_PLATE", "EIUY09")

    async def run():
        async with Client(_params(), read_timeout_seconds=180) as client:
            r = await client.call_tool("ctio_list_notices", {"plate": plate})
            assert not r.is_error, _text(r)
            sc = r.structured_content
            assert sc["plate"] == plate and sc["notices"], sc
            print("live list:", {k: sc[k] for k in ("site_vehicle", "fleet_vehicle", "total_due", "open_notice_count")}, sc["notices"])

            r = await client.call_tool("ctio_lookup_notice", {"notice_number": "123-456-789-012", "plate": "ZZZ000"})
            assert r.is_error and "do not match" in _text(r)

    anyio.run(run)
