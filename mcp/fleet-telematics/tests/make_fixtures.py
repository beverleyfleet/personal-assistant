"""Generate Smartcar v3 fixtures shaped from the documented OpenAPI examples (docs read 2026-10-24)."""
import json, sys
from pathlib import Path
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
GROUP = {"odometer":"Odometer","internalcombustionengine":"InternalCombustionEngine","location":"Location","closure":"Closure",
         "wheel":"Wheel","diagnostics":"Diagnostics","lowvoltagebattery":"LowVoltageBattery","transmission":"Transmission",
         "motion":"Motion","connectivitystatus":"ConnectivityStatus","vehicleidentification":"VehicleIdentification"}
IDS = {"t24":"3f1c2b7e-24aa-4c1e-9a10-000000000024","t25":"3f1c2b7e-25aa-4c1e-9a10-000000000025",
       "audi":"a0d1a0d1-0000-4a11-8e22-0000000a0d10","sub":"5b5b5b5b-0000-4c0e-8a33-000000005b00"}
USERS = {"ford":"11111111-f0f0-4d00-a000-00000000f04d","audi":"22222222-a0a0-4d00-a000-00000000a0d1",
         "chevrolet":"33333333-c0c0-4d00-a000-00000000c4e7"}
VEH = {"t24":("Ford","Transit",2024,"1FBAX9Y87RKA27765","ford"),"t25":("Ford","Transit",2025,"1FBAX9CGXSKA05649","ford"),
       "audi":("Audi","Q5",2022,"WA1GAAFY6N2085527","audi"),"sub":("Chevrolet","Suburban",2021,"1GNSKHKD5MR000001","chevrolet")}
def vres(k):
    mk, md, yr, _, _ = VEH[k]
    return {"id":IDS[k],"type":"vehicle","attributes":{"make":mk,"model":md,"year":yr,"powertrainType":"ICE","mode":"live"},
            "links":{"self":f"/vehicles/{IDS[k]}"}}
def sig(code, body, ts="__HOURS_AGO_0__", status="SUCCESS", error=None):
    g = GROUP[code.split("-")[0]]
    st = {"value":status}
    if error: st["error"]=error
    s = {"id":code,"type":"signal","attributes":{"code":code,"name":code.split("-",1)[1],"group":g,"status":st},
         "meta":{"retrievedAt":"__HOURS_AGO_0__","oemUpdatedAt":ts,"ingestedAt":"__HOURS_AGO_0__"},"links":{"self":f"/vehicles/x/signals/{code}"}}
    if body is not None: s["attributes"]["body"]=body
    return s
def doors(locked, open_=()):
    return {"rowCount":2,"columnCount":2,"values":[{"row":r,"column":c,"isOpen":(r,c) in open_,"isLocked":locked} for r in (0,1) for c in (0,1)]}
def tires(kpa):
    return {"rowCount":2,"columnCount":2,"unit":"kPa","values":[{"row":r,"column":c,"tirePressure":kpa[i]} for i,(r,c) in enumerate([(0,0),(0,1),(1,0),(1,1)])]}
NOT_CAPABLE = {"type":"COMPATIBILITY","code":"VEHICLE_NOT_CAPABLE","title":"Vehicle Not Capable","detail":"The vehicle is not capable of performing the request."}
PARK = (39.8181, -104.6889)
data = {
 "t24":[sig("odometer-traveleddistance",{"value":28311.4,"unit":"km"}),
        sig("internalcombustionengine-fuellevel",{"value":62,"unit":"percent"}),
        sig("internalcombustionengine-range",{"value":402,"unit":"km"}),
        sig("internalcombustionengine-amountremaining",{"value":62.5,"unit":"liters"}),
        sig("location-preciselocation",{"latitude":PARK[0],"longitude":PARK[1],"heading":90.0,"direction":"E","locationType":"LAST_PARKED"}),
        sig("closure-islocked",{"value":True}), sig("closure-doors",doors(True)),
        sig("wheel-tires",tires([379,379,393,393])),
        sig("internalcombustionengine-oillife",{"value":48,"unit":"percent"}),
        sig("transmission-gearstate",{"value":"PARK"}), sig("vehicleidentification-vin",{"value":"1FBAX9Y87RKA27765"})],
 "t25":[sig("odometer-traveleddistance",{"value":9120,"unit":"miles"}),
        sig("internalcombustionengine-fuellevel",{"value":18,"unit":"percent"}),
        sig("internalcombustionengine-range",{"value":96,"unit":"km"}),
        sig("location-preciselocation",{"latitude":39.8175,"longitude":-104.6902,"heading":0,"direction":"N","locationType":"LAST_PARKED"}),
        sig("closure-islocked",{"value":False}), sig("closure-doors",doors(False,[(1,1)])),
        sig("wheel-tires",tires([380,381,395,394])),
        sig("internalcombustionengine-oillife",{"value":81,"unit":"percent"}),
        sig("vehicleidentification-vin",{"value":"1FBAX9CGXSKA05649"})],
 "audi":[sig("odometer-traveleddistance",{"value":41000,"unit":"km"}),
        sig("internalcombustionengine-fuellevel",{"value":40,"unit":"percent"}),
        sig("internalcombustionengine-range",{"value":250,"unit":"km"}),
        sig("location-preciselocation",{"latitude":39.7392,"longitude":-104.9903,"heading":180,"direction":"S","locationType":"CURRENT"}),
        sig("closure-islocked",{"value":True}),
        sig("wheel-tires",None,status="ERROR",error=NOT_CAPABLE),
        sig("vehicleidentification-vin",{"value":"WA1GAAFY6N2085527"})],
 "sub":[sig("odometer-traveleddistance",{"value":61234,"unit":"miles"},ts="__HOURS_AGO_72__"),
        sig("internalcombustionengine-fuellevel",{"value":80,"unit":"percent"},ts="__HOURS_AGO_72__"),
        sig("internalcombustionengine-range",{"value":560,"unit":"km"},ts="__HOURS_AGO_72__"),
        sig("location-preciselocation",{"latitude":39.8183,"longitude":-104.6880,"locationType":"LAST_PARKED"},ts="__HOURS_AGO_72__"),
        sig("wheel-tires",tires([241,241,248,248]),ts="__HOURS_AGO_72__"),
        sig("internalcombustionengine-oillife",{"value":9,"unit":"percent"},ts="__HOURS_AGO_72__"),
        sig("diagnostics-oillife",{"status":"ALERT","description":"Change oil soon"},ts="__HOURS_AGO_72__"),
        sig("diagnostics-tirepressure",{"status":"OK","description":""},ts="__HOURS_AGO_72__"),
        sig("vehicleidentification-vin",{"value":"1GNSKHKD5MR000001"},ts="__HOURS_AGO_72__")],
}
for k, sigs in data.items():
    (out/f"signals_{IDS[k]}.json").write_text(json.dumps({"data":sigs,"meta":{"pageNumber":1,"pageSize":len(sigs),"totalCount":len(sigs)},
        "links":{"self":f"https://vehicle.api.smartcar.com/v3/vehicles/{IDS[k]}/signals"},"included":{"vehicle":vres(k)}},indent=1))
    (out/f"vehicle_{IDS[k]}.json").write_text(json.dumps({"data":vres(k)},indent=1))
conns=[]
for k in IDS:
    mk, md, yr, vin, acct = VEH[k]
    conns.append({"id":f"c0nn-{k}","type":"connection","attributes":{"permissions":["read_vehicle_info","read_vin","read_odometer","read_fuel","read_location","read_security"],
        "vehicle":{"make":mk,"model":md,"year":yr,"mode":"live","powertrainType":"ICE"},"user":{"id":USERS[acct],"externalId":f"beverley-fleet-{acct}"},
        "status":{"name":"ACTIVE"}},"relationships":{"vehicle":{"data":{"id":IDS[k],"type":"vehicle"},"links":{"self":f"/vehicles/{IDS[k]}"}}}})
(out/"connections.json").write_text(json.dumps({"data":conns,"meta":{"pageNumber":1,"pageSize":50,"totalCount":len(conns),"orderBy":"createdAt","orderDirection":"DESC"},
    "links":{"self":"https://vehicle.api.smartcar.com/v3/connections?page[number]=1&page[size]=50","next":None}},indent=1))
(out/"token.json").write_text(json.dumps({"access_token":"replay-token-not-a-secret","token_type":"Bearer","expires_in":3600}))
(out/"command_ok.json").write_text(json.dumps({"data":{"id":"exec_9876543210","type":"command-execution","attributes":{"commandType":"__COMMAND__","status":{"value":"SUCCESS"},"executionMode":"sync"},
    "meta":{"executedAt":"2026-10-24T12:00:00Z","completedAt":"2026-10-24T12:00:02Z","durationInSeconds":2}}},indent=1))
(out/"ids.json").write_text(json.dumps({"vehicles":IDS,"users":USERS},indent=1))
print(sorted(p.name for p in out.iterdir()))
