"""Generate Turo welcome messages from the booking list (calendar + Rental_Agreements_Tracker)."""
import json, sys

VEHICLES = {
    "audi":   dict(name="2022 Audi Q5 (gray)", fuel="Premium", access="lockbox", clearance=None, seats="5"),
    "sub":    dict(name="2021 Chevrolet Suburban (white)", fuel="Regular unleaded", access="lockbox", clearance=None, seats="7"),
    "t23":    dict(name="2023 Ford Transit-350 Med Roof (white)", fuel="Regular unleaded", access="lockbox", clearance=True, seats="15 (incl. driver)"),
    "t24":    dict(name="2024 Ford Transit-350 Low Roof (white)", fuel="Regular unleaded", access="remote", clearance=None, seats="15"),
    "t25":    dict(name="2025 Ford Transit-350 Med Roof (white)", fuel="Regular unleaded", access="remote", clearance=True, seats="15"),
    "sprinter": dict(name="2026 Mercedes-Benz Sprinter (black)", fuel="Diesel", access="remote", clearance=True, seats="15"),
}

ACCESS = {
    "lockbox": "the lockbox code (the key is in a black bag inside the lockbox)",
    "remote": "remote-unlock instructions (the van unlocks remotely, so no key handoff is needed)",
}

def message(guest, vkey, pickup, dropoff):
    v = VEHICLES[vkey]
    lines = [
        f"Hi {guest}! Thank you for booking our {v['name']} with Beverley Fleet. We're excited to host you {pickup} – {dropoff}.",
        "",
        "Here's what to know before pickup:",
        "",
        "• WHERE: ParkDIA, 25200 E 68th Ave, Aurora, CO 80019. It's the lot right next to Denver International Airport.",
        "• FROM THE TERMINAL: Go to Level 5, exit Door 513 on the EAST side, and go to Island 3 at the ParkDIA sign. The free white ParkDIA shuttle runs 24/7 and takes about 10 minutes. Tell the driver you're a Turo guest picking up a rental. (Heads up: from Nov 3 the west-side stop is closed. Pickups and drop-offs are east side only.)",
        f"• WITHIN 2 HOURS OF PICKUP: We'll message you the exact row/spot and {ACCESS[v['access']]}. ParkDIA is a big lot, and the shuttle will drop you right at the vehicle.",
        "• EXITING: The RFID sticker on the windshield opens the gate automatically.",
        f"• FUEL: {v['fuel']}. Please return it with the same fuel level it had at pickup.",
    ]
    if v["clearance"]:
        lines.append("• HEIGHT: No parking garages or drive-thrus. This van is tall and can scrape.")
    lines += [
        "• RETURN: Same ParkDIA lot. We'll send step-by-step drop-off instructions the day before your return.",
        "",
        "If you have any questions before then, just reply here. Safe travels!",
        "",
        "Cristin, Beverley Fleet",
    ]
    return "\n".join(lines)

if __name__ == "__main__":
    bookings = json.load(open(sys.argv[1]))
    out = [dict(b, body=message(b["guest"], b["vehicle"], b["pickup"], b["dropoff"])) for b in bookings]
    json.dump(out, open(sys.argv[2], "w"), indent=1)
    print(out[0]["body"]); print("---", len(out), "messages")
