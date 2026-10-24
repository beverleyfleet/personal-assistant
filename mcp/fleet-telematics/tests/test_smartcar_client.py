"""SmartcarClient against httpx2.MockTransport: request shapes follow the v3 docs."""
import json
from urllib.parse import parse_qs

import anyio
import httpx2
import pytest

from fleet_telematics.providers.smartcar import (SmartcarAPIError, SmartcarClient, _to_miles, _to_psi, normalize)

from conftest import FIX, IDS, SECRET

T24 = IDS["vehicles"]["t24"]
FORD_USER = IDS["users"]["ford"]


def mk(handler):
    return SmartcarClient(transport=httpx2.MockTransport(handler), client_id="cid", client_secret=SECRET)


def token_resp(request):
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert request.url == "https://iam.smartcar.com/oauth2/token"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": SECRET}
    return httpx2.Response(200, json={"access_token": "tok1", "token_type": "Bearer", "expires_in": 3600})


def test_token_cached_and_signals_headers():
    seen = []

    def h(req):
        seen.append(req)
        if req.url.host == "iam.smartcar.com":
            return token_resp(req)
        assert str(req.url) == f"https://vehicle.api.smartcar.com/v3/vehicles/{T24}/signals"
        assert req.headers["authorization"] == "Bearer tok1"
        assert req.headers["sc-user-id"] == FORD_USER
        return httpx2.Response(200, text=(FIX / f"signals_{T24}.json").read_text().replace("__HOURS_AGO_0__", "2026-10-24T12:00:00Z"))

    async def run():
        c = mk(h)
        a = await c.signals(T24, FORD_USER)
        b = await c.signals(T24, FORD_USER)
        await c.aclose()
        return a, b

    a, b = anyio.run(run)
    assert "odometer-traveleddistance" in a and a.keys() == b.keys()
    assert sum(1 for r in seen if r.url.host == "iam.smartcar.com") == 1  # token reused


def test_401_refetches_token_once():
    tokens = iter(["old", "new"])
    calls = []

    def h(req):
        if req.url.host == "iam.smartcar.com":
            return httpx2.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        calls.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer old":
            return httpx2.Response(401, json={"errors": [{"status": "401", "type": "AUTHENTICATION", "code": "UNAUTHORIZED",
                                                          "title": "Unauthorized", "detail": "x"}]})
        return httpx2.Response(200, json={"data": {"id": T24, "type": "vehicle", "attributes": {"make": "Ford", "model": "Transit", "year": 2024}}})

    async def run():
        c = mk(h)
        try:
            return await c.get_vehicle(T24)
        finally:
            await c.aclose()

    assert anyio.run(run)["attributes"]["make"] == "Ford"
    assert calls == ["Bearer old", "Bearer new"]


@pytest.mark.parametrize("status,body,headers,expect", [
    (429, {"errors": [{"status": "429", "type": "RATE_LIMIT", "code": "VEHICLE", "title": "Too Many Requests",
                       "detail": "throttled", "resolution": {"type": "RETRY_LATER"}}]}, {"retry-after": "120"},
     ["429", "RATE_LIMIT:VEHICLE", "retry after 120s"]),
    (401, {"errors": [{"status": "401", "type": "CONNECTED_SERVICES_ACCOUNT", "code": "AUTHENTICATION_FAILED",
                       "title": "t", "detail": "OEM login failed", "resolution": {"type": "REAUTHENTICATE"}}]}, {},
     ["fleet-auth connect"]),
])
def test_error_mapping(status, body, headers, expect):
    def h(req):
        if req.url.host == "iam.smartcar.com":
            return token_resp(req)
        return httpx2.Response(status, json=body, headers=headers)

    async def run():
        c = mk(h)
        try:
            await c.signals(T24, FORD_USER)
        finally:
            await c.aclose()

    with pytest.raises(SmartcarAPIError) as ei:
        anyio.run(run)
    for e in expect:
        assert e in str(ei.value)
    assert SECRET not in str(ei.value)


def test_command_failure_in_body_raises():
    def h(req):
        if req.url.host == "iam.smartcar.com":
            return token_resp(req)
        assert req.method == "POST" and req.url.path == f"/v3/vehicles/{T24}/commands/security/unlock"
        assert req.content == b""  # lock/unlock take no body
        return httpx2.Response(202, json={"data": {"id": "exec_1", "type": "command-execution", "attributes": {
            "commandType": "security-unlock", "executionMode": "sync", "status": {"value": "FAILURE", "error": {
                "status": "409", "type": "VEHICLE_STATE", "code": "IN_MOTION", "title": "In motion", "detail": "Vehicle is moving"}}},
            "meta": {"executedAt": "x", "completedAt": "y", "durationInSeconds": 190}}})

    async def run():
        c = mk(h)
        try:
            await c.command(T24, FORD_USER, "security/unlock")
        finally:
            await c.aclose()

    with pytest.raises(SmartcarAPIError, match="IN_MOTION"):
        anyio.run(run)


def test_connections_pagination_and_filters():
    pages = []

    def h(req):
        if req.url.host == "iam.smartcar.com":
            return token_resp(req)
        q = dict(req.url.params)
        pages.append(q)
        assert q["filter[userId]"] == FORD_USER and q["page[size]"] == "50"
        n = int(q["page[number]"])
        item = {"id": f"c{n}", "relationships": {"vehicle": {"data": {"id": f"v{n}"}}}}
        return httpx2.Response(200, json={"data": [item], "meta": {"totalCount": 2}})

    async def run():
        c = mk(h)
        try:
            return await c.list_connections(user_id=FORD_USER)
        finally:
            await c.aclose()

    assert [c["id"] for c in anyio.run(run)] == ["c1", "c2"]


def test_truncated_signal_list_falls_back_to_single_gets():
    def h(req):
        if req.url.host == "iam.smartcar.com":
            return token_resp(req)
        if req.url.path.endswith("/signals"):
            one = {"id": "closure-islocked", "type": "signal", "attributes": {"code": "closure-islocked", "name": "IsLocked",
                   "group": "Closure", "status": {"value": "SUCCESS"}, "body": {"value": True}}, "meta": {}}
            return httpx2.Response(200, json={"data": [one], "meta": {"pageNumber": 1, "pageSize": 1, "totalCount": 30}})
        code = req.url.path.rsplit("/", 1)[-1]
        if code == "odometer-traveleddistance":
            return httpx2.Response(200, json={"data": {"id": code, "type": "signal", "attributes": {
                "code": code, "name": "TraveledDistance", "group": "Odometer", "status": {"value": "SUCCESS"},
                "body": {"value": 1609.344, "unit": "km"}}, "meta": {}}})
        return httpx2.Response(404, json={"errors": [{"status": "404", "type": "RESOURCE_NOT_FOUND", "code": "NOT_FOUND",
                                                      "title": "Not Found", "detail": "x"}]})

    async def run():
        c = mk(h)
        try:
            return await c.signals(T24, FORD_USER)
        finally:
            await c.aclose()

    sigs = anyio.run(run)
    assert normalize(sigs)["odometer"]["miles"] == 1000.0
    assert normalize(sigs)["lock"]["is_locked"] is True


def test_unit_conversions_and_normalize_variants():
    assert _to_miles(160.9344, "km") == 100.0 and _to_miles(42, "miles") == 42
    assert _to_psi(241.3, "kPa") == 35.0 and _to_psi(2.413, "bar") == 35.0 and _to_psi(35, "psi") == 35
    # docs show closure-doors with `values` in the schema but `doors` in the example: accept both; no islocked signal
    doors = {"code": "closure-doors", "status": {"value": "SUCCESS"},
             "body": {"rowCount": 1, "columnCount": 2, "doors": [{"row": 0, "column": 0, "isLocked": False, "isOpen": True},
                                                                {"row": 0, "column": 1, "isLocked": True, "isOpen": False}]}}
    n = normalize({"closure-doors": {"attributes": doors, "meta": {}}})
    assert n["lock"] == {"is_locked": False, "source": "closure-doors", "doors_open": ["row0_col0"], "as_of": None}
    assert n["fuel"] is None and n["signals"]["internalcombustionengine-fuellevel"]["status"] == "NOT_RETURNED"
