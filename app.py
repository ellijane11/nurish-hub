# app.py
# =============================================================================
# 🍲 FOOD IS HOPE — Surplus Food Donation Platform (Streamlit)
# =============================================================================
# Key features in this version:
#   1) Donor notifications ("Accepted", "Picked Up") can be MARKED AS SEEN and
#      will no longer pop up every time for that user.
#   2) Collector can CANCEL an accepted donation (returns it to 'active').
#   3) Robust button keys to avoid StreamlitDuplicateElementKey errors.
#   4) Backward-compatible data migration for newly introduced fields:
#        - donation.status timestamps (created_at, accepted_at, picked_up_at, cancelled_at)
#        - donation.collector_name / collector_phone
#        - users[phone].seen.donor / users[phone].seen.collector structure
#   5) Cleaned logic & helpers (gmaps links, time formatting, etc.)
#   6) Map-click exact location selection for donors (added in this version)
#   7) Address-first UX: donors type address → app geocodes and centers map → donor clicks to fine-tune
#   8) Quantity field: donors enter quantity (e.g., "10 meals", "5 kg") — saved & displayed
#
# Data files:
#   - donations.json
#   - users.json
#
# Author: You ✨
# =============================================================================

import streamlit as st
from streamlit_folium import st_folium
import folium
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import json
import os
import time
import hashlib
from uuid import uuid4
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# -----------------------------------------------------------------------------
# APP CONFIG
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Food Donation App", page_icon="🍲", layout="centered")

# -----------------------------------------------------------------------------
# CONSTANTS / FILES
# -----------------------------------------------------------------------------
DATA_FILE = "donations.json"
USERS_FILE = "users.json"
NEARBY_RADIUS_KM = 10  # show donors within this radius of the collector (km)

# Notification event labels
DONOR_EVENTS = ("accepted", "picked_up", "cancelled")
COLLECTOR_EVENTS = ("assigned", "unassigned")  # future-reserved (not shown now)

# -----------------------------------------------------------------------------
# HELPERS: TIME, HASH, LINKS
# -----------------------------------------------------------------------------
def now_ts() -> int:
    return int(time.time())

def fmt_time(ts: Optional[int]) -> str:
    """Format a unix timestamp (seconds) to a human-readable local string."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"

def hash_password(password: str) -> str:
    """Return SHA256 hashed password."""
    return hashlib.sha256(password.encode()).hexdigest()

def gmaps_dir_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"

# -----------------------------------------------------------------------------
# STORAGE: LOAD / SAVE
# -----------------------------------------------------------------------------
def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path: str, obj: Any):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4)
    os.replace(tmp, path)

def load_donations() -> List[Dict[str, Any]]:
    """Load donations, ensure required fields, fix duplicate IDs, and add status/timestamps if missing."""
    donations = load_json(DATA_FILE, [])
    seen_ids = set()
    changed = False

    for d in donations:
        # Ensure robust unique id
        if "id" not in d or d["id"] in seen_ids or not d["id"]:
            d["id"] = f"{d.get('phone','')}{int(time.time()*1_000_000)}{uuid4().hex[:6]}"
            changed = True
        seen_ids.add(d["id"])

        # Ensure status field
        if "status" not in d:
            d["status"] = "active"
            changed = True

        # Ensure new fields exist (for compatibility)
        d.setdefault("collector_name", None)
        d.setdefault("collector_phone", None)

        # Ensure timestamps
        d.setdefault("created_at", now_ts())
        d.setdefault("accepted_at", None)
        d.setdefault("picked_up_at", None)
        d.setdefault("cancelled_at", None)

        # Ensure lat/lon if present are numbers (avoid strings sneaking in)
        if "lat" in d and isinstance(d["lat"], str):
            try:
                d["lat"] = float(d["lat"])
                changed = True
            except Exception:
                d["lat"] = None
                changed = True
        if "lon" in d and isinstance(d["lon"], str):
            try:
                d["lon"] = float(d["lon"])
                changed = True
            except Exception:
                d["lon"] = None
                changed = True

        # Ensure quantity field exists for compatibility (new feature)
        if "quantity" not in d:
            d["quantity"] = None
            changed = True

    if changed:
        save_json(DATA_FILE, donations)
    return donations

def save_donations(donations: List[Dict[str, Any]]):
    save_json(DATA_FILE, donations)

def load_users() -> Dict[str, Any]:
    """Users file structure:
    {
      "<phone>": {
        "name": "...",
        "email": "...",
        "password": "<sha256>",
        "seen": {
            "donor": { "<donation_id>": { "accepted": true, "picked_up": true, "cancelled": true } },
            "collector": { "<donation_id>": { "assigned": true, "unassigned": true } }
        }
      },
      ...
    }
    """
    users = load_json(USERS_FILE, {})
    changed = False
    for phone, rec in users.items():
        if "seen" not in rec or not isinstance(rec["seen"], dict):
            rec["seen"] = {"donor": {}, "collector": {}}
            changed = True
        else:
            rec["seen"].setdefault("donor", {})
            rec["seen"].setdefault("collector", {})
    if changed:
        save_json(USERS_FILE, users)
    return users

def save_users(users: Dict[str, Any]):
    save_json(USERS_FILE, users)

# -----------------------------------------------------------------------------
# SESSION STATE INIT
# -----------------------------------------------------------------------------
if "donations" not in st.session_state:
    st.session_state.donations = load_donations()

if "users" not in st.session_state:
    st.session_state.users = load_users()

if "page" not in st.session_state:
    st.session_state.page = "login"

if "user" not in st.session_state:
    st.session_state.user = None  # {"name":..., "phone":..., "email":...}

if "collector_coords" not in st.session_state:
    st.session_state.collector_coords = None  # (lat, lon, label)

# -----------------------------------------------------------------------------
# GLOBALS
# -----------------------------------------------------------------------------
geolocator = Nominatim(user_agent="food_is_hope")

def update_donations():
    save_donations(st.session_state.donations)

def update_users():
    save_users(st.session_state.users)

# -----------------------------------------------------------------------------
# NOTIFICATION "SEEN" HELPERS
# -----------------------------------------------------------------------------
def ensure_user_seen(phone: str):
    users = st.session_state.users
    if phone not in users:
        return
    u = users[phone]
    if "seen" not in u or not isinstance(u["seen"], dict):
        u["seen"] = {"donor": {}, "collector": {}}
    else:
        u["seen"].setdefault("donor", {})
        u["seen"].setdefault("collector", {})

def mark_seen(phone: str, role_bucket: str, donation_id: str, event: str):
    """role_bucket: 'donor' or 'collector'"""
    users = st.session_state.users
    if phone not in users:
        return
    ensure_user_seen(phone)
    users[phone]["seen"][role_bucket].setdefault(donation_id, {})
    users[phone]["seen"][role_bucket][donation_id][event] = True
    update_users()

def is_seen(phone: str, role_bucket: str, donation_id: str, event: str) -> bool:
    users = st.session_state.users
    if phone not in users:
        return False
    seen = users[phone].get("seen", {}).get(role_bucket, {})
    return bool(seen.get(donation_id, {}).get(event, False))

def clear_seen_for_donation(phone: str, role_bucket: str, donation_id: str):
    """Optional helper to clear all events for one donation."""
    users = st.session_state.users
    if phone not in users:
        return
    if "seen" in users[phone] and role_bucket in users[phone]["seen"]:
        users[phone]["seen"][role_bucket].pop(donation_id, None)
        update_users()

# -----------------------------------------------------------------------------
# AUTH / LOGIN PAGE (single form)
# -----------------------------------------------------------------------------
gmail_pattern = r"^[a-zA-Z0-9._%+-]+@gmail\.com$"

def login_page():
    st.markdown("<h1 style='text-align: center;'>🍽 FOOD IS HOPE</h1>", unsafe_allow_html=True)
    st.markdown("<h3 style='text-align: center;'>LOGIN or REGISTER</h3>", unsafe_allow_html=True)

    with st.form("login_form_unique"):
        phone = st.text_input("📱 Phone Number (10 digits)")
        password = st.text_input("🔑 Password", type="password")
        st.caption("If you're a new user, please also provide your name and Gmail below to register.")
        name = st.text_input("👤 Name (for new users)")
        email = st.text_input("✉ Gmail (for new users)")

        # Live Gmail validation
        if email:
            if re.match(gmail_pattern, email, re.IGNORECASE):
                st.success("✅ Valid Gmail address")
            else:
                st.warning("⚠ Please enter a valid Gmail address (like example@gmail.com)")

        submitted = st.form_submit_button("Login / Register")

    if submitted:
        if not (phone and password):
            st.error("⚠ Please enter both phone and password.")
            return
        if not (phone.isdigit() and len(phone) == 10):
            st.error("⚠ Invalid phone number. Please enter a 10-digit number.")
            return

        users = st.session_state.users

        # Existing user
        if phone in users:
            if users[phone]["password"] == hash_password(password):
                st.session_state.user = {
                    "name": users[phone]["name"],
                    "phone": phone,
                    "email": users[phone]["email"],
                }
                ensure_user_seen(phone)
                st.session_state.page = "role_select"
                st.success("✅ Login successful!")
                st.rerun()
            else:
                st.error("❌ Incorrect password.")
        else:
            # Registration path
            if not (name and email):
                st.error("⚠ New user detected. Please provide Name and Gmail to register.")
                return
            if not re.match(gmail_pattern, email or "", re.IGNORECASE):
                st.error("⚠ Please register with a valid Gmail address (like example@gmail.com).")
                return
            users[phone] = {
                "name": name,
                "email": email,
                "password": hash_password(password),
                "seen": {"donor": {}, "collector": {}},
            }
            update_users()
            st.session_state.user = {
                "name": name,
                "phone": phone,
                "email": email,
            }
            st.session_state.page = "role_select"
            st.success("🎉 Registration successful! You are now logged in.")
            st.rerun()

# -----------------------------------------------------------------------------
# ROLE SELECT PAGE
# -----------------------------------------------------------------------------
def role_select_page():
    st.success(f"✅ Welcome, {st.session_state.user['name']}! Please choose your role:")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🍎 Donor", use_container_width=True):
            st.session_state.page = "donor_page"
            st.rerun()
    with col2:
        if st.button("🚚 Collector", use_container_width=True):
            st.session_state.page = "collector_page"
            st.rerun()
    with col3:
        if st.button("🤝 Community", use_container_width=True):
            st.session_state.page = "community_page"
            st.rerun()

    if st.button("Logout"):
        st.session_state.page = "login"
        st.session_state.user = None
        st.session_state.collector_coords = None
        st.rerun()

# -----------------------------------------------------------------------------
# DONOR PAGE
# -----------------------------------------------------------------------------
def donor_page():
    st.header("🍎 Donor Dashboard")

    phone = st.session_state.user["phone"]
    my_donations = [d for d in st.session_state.donations if d.get("phone") == phone]
    accepted = [d for d in my_donations if d.get("status") == "accepted"]
    picked_up = [d for d in my_donations if d.get("status") == "picked_up"]
    active_donations = [d for d in my_donations if d.get("status", "active") == "active"]
    cancelled = [d for d in my_donations if d.get("status") == "cancelled"]

    # ---------------------------
    # Notifications — ACCEPTED
    # ---------------------------
    if accepted:
        st.subheader("🤝 Accepted by Collector")
        for idx, d in enumerate(sorted(accepted, key=lambda x: x.get("accepted_at") or 0, reverse=True)):
            cname = d.get("collector_name") or "a collector"
            cphone = d.get("collector_phone") or "N/A"
            when = fmt_time(d.get("accepted_at"))
            did = d["id"]
            seen_key = f"seen_accept_{did}_{idx}"
            is_event_seen = is_seen(phone, "donor", did, "accepted")

            if not is_event_seen:
                st.info(
                    f"Your donation *{d.get('food','?')}* ({d.get('quantity','?')}) at *{d.get('location','?')}* "
                    f"was *accepted* by *{cname}* (📞 {cphone}) at {when}."
                )
                if st.button("Mark as seen", key=seen_key):
                    mark_seen(phone, "donor", did, "accepted")
                    st.rerun()
            else:
                with st.expander(f"Seen: {d.get('food','?')} accepted by {cname} at {when}"):
                    st.write("You have marked this notification as seen.")
                    if st.button("Unhide (show again)", key=f"unsee_accept_{did}_{idx}"):
                        # Just clear this single 'accepted' flag
                        users = st.session_state.users
                        users[phone]["seen"]["donor"].setdefault(did, {})
                        users[phone]["seen"]["donor"][did].pop("accepted", None)
                        update_users()
                        st.rerun()

    # ---------------------------
    # Notifications — PICKED UP
    # ---------------------------
    if picked_up:
        st.subheader("✅ Picked Up")
        for idx, d in enumerate(sorted(picked_up, key=lambda x: x.get("picked_up_at") or 0, reverse=True)):
            cname = d.get("collector_name") or "Collector"
            when = fmt_time(d.get("picked_up_at"))
            did = d["id"]
            is_event_seen = is_seen(phone, "donor", did, "picked_up")
            seen_key = f"seen_pu_{did}_{idx}"

            if not is_event_seen:
                st.success(
                    f"Your donation *{d.get('food','?')}* ({d.get('quantity','?')}) at *{d.get('location','?')}* "
                    f"was *picked up* by *{cname}* at {when}!"
                )
                if st.button("Mark as seen", key=seen_key):
                    mark_seen(phone, "donor", did, "picked_up")
                    st.rerun()
            else:
                with st.expander(f"Seen: {d.get('food','?')} picked up by {cname} at {when}"):
                    st.write("You have marked this notification as seen.")
                    if st.button("Unhide (show again)", key=f"unsee_pu_{did}_{idx}"):
                        users = st.session_state.users
                        users[phone]["seen"]["donor"].setdefault(did, {})
                        users[phone]["seen"]["donor"][did].pop("picked_up", None)
                        update_users()
                        st.rerun()

    # ---------------------------
    # ACTIVE DONATIONS
    # ---------------------------
    if active_donations:
        st.subheader("Your Active Donations")
        for idx, d in enumerate(sorted(active_donations, key=lambda x: x.get("created_at") or 0, reverse=True)):
            st.info(
                f"🍲 {d.get('food','?')} • {d.get('quantity','?')} | 📍 {d.get('location','?')} | ⏳ {d.get('availability','?')} | "
                f"🕒 Created: {fmt_time(d.get('created_at'))}"
            )
            col1, col2 = st.columns(2)
            with col1:
                # Robust unique key to avoid duplicate
                if st.button(
                    f"❌ Cancel '{d.get('food','item')}'",
                    key=f"cancel_{d.get('id','noid')}_{idx}"
                ):
                    # Mark as cancelled (do not delete to preserve history)
                    for dd in st.session_state.donations:
                        if dd["id"] == d["id"]:
                            dd["status"] = "cancelled"
                            dd["cancelled_at"] = now_ts()
                    update_donations()
                    st.rerun()
            with col2:
                st.write("")

    # ---------------------------
    # CANCELLED DONATIONS
    # ---------------------------
    if cancelled:
        st.subheader("🗂 Cancelled Donations")
        for idx, d in enumerate(sorted(cancelled, key=lambda x: x.get("cancelled_at") or x.get("created_at") or 0, reverse=True)):
            when = fmt_time(d.get("cancelled_at"))
            did = d["id"]
            # Optional donor 'cancelled' notification seen flag
            if not is_seen(phone, "donor", did, "cancelled"):
                st.warning(
                    f"Cancelled: {d.get('food','?')} • {d.get('quantity','?')} • {d.get('location','?')} • "
                    f"🕒 Cancelled: {when}"
                )
                if st.button("Mark as seen", key=f"seen_cancel_{did}_{idx}"):
                    mark_seen(phone, "donor", did, "cancelled")
                    st.rerun()
            else:
                with st.expander(f"Seen: Cancelled donation — {d.get('food','?')} at {when}"):
                    st.write("You have marked this cancellation as seen.")
                    if st.button("Unhide (show again)", key=f"unsee_cancel_{did}_{idx}"):
                        users = st.session_state.users
                        users[phone]["seen"]["donor"].setdefault(did, {})
                        users[phone]["seen"]["donor"][did].pop("cancelled", None)
                        update_users()
                        st.rerun()

    # ---------------------------
    # ADD NEW DONATION (UPDATED: address-first + map-click adjust + quantity)
    # ---------------------------
    st.write("---")
    st.subheader("Add a New Donation")

    # We'll display a little two-step UX in the same form:
    # 1) User types address (location_name). We attempt to geocode it to center the map.
    # 2) Map is shown centered on geocoded point (or fallback). Donor clicks to fine-tune.
    # 3) On Save, we prioritize clicked coordinates, otherwise use geocoded coords.

    with st.form("donor_form_main"):
        food_item = st.text_input("🍲 Food Item")
        quantity_text = st.text_input("📦 Quantity (e.g. '10 meals', '5 kg rice', '20 boxes')")  # NEW FIELD
        availability = st.text_input("📅 Available Until (e.g. 'Tonight 9 PM')")
        location_name = st.text_input("📍 Enter Your Location (required, e.g., 'MG Road, Bangalore')")

        st.markdown("**📍 Recommended:** First type a specific address/landmark; the map will center there. Then click the exact pickup spot on the map to fine-tune.")
        st.markdown("If you leave the map un-clicked, we'll use the geocoded location as the pickup point (if geocoding succeeded).")

        # Determine map center: try geocoding the typed location for a nicer initial view.
        # We compute this inside the form so the user sees the centered map while the form is open.
        default_center = [12.9716, 77.5946]  # Bangalore fallback
        map_center = default_center
        geocoded_lat = None
        geocoded_lon = None
        geocoded_address_display = None

        try:
            if location_name and location_name.strip():
                # Try geocoding with appended country to improve accuracy
                safe_query = location_name.strip()
                try:
                    geocoded = geolocator.geocode(safe_query + ", India")
                except Exception:
                    # if the above fails (rate limit or other), try plain query
                    geocoded = None
                    try:
                        geocoded = geolocator.geocode(safe_query)
                    except Exception:
                        geocoded = None

                if geocoded:
                    geocoded_lat = geocoded.latitude
                    geocoded_lon = geocoded.longitude
                    geocoded_address_display = getattr(geocoded, "address", None)
                    map_center = [geocoded_lat, geocoded_lon]
        except Exception:
            # ignore geocode errors here; we'll try again on submit
            map_center = default_center
            geocoded_lat = None
            geocoded_lon = None

        # Build the map centered at map_center, show a suggested marker when geocoded is available.
        zoom_level = 14 if map_center != default_center else 5
        m = folium.Map(location=map_center, zoom_start=zoom_level)

        if geocoded_lat and geocoded_lon:
            # add a suggested marker
            folium.Marker(
                [geocoded_lat, geocoded_lon],
                tooltip="Suggested location (from address). Click the map to fine-tune.",
                popup=geocoded_address_display or location_name
            ).add_to(m)

        st.write("(Click once on the map to set the final pickup point. If you click, the pin will use the clicked coordinates.)")
        map_data = st_folium(m, height=380, width=700)

        submitted = st.form_submit_button("Save Donation")

    # Handle submit outside the form block
    if submitted:
        if not (food_item and quantity_text and availability and location_name):
            st.error("⚠ Please fill all required fields (food item, quantity, availability, and location text or use the map click).")
        else:
            chosen_lat = None
            chosen_lon = None

            # 1) Priority: use last map click if present
            try:
                if map_data and map_data.get("last_clicked"):
                    chosen_lat = map_data["last_clicked"]["lat"]
                    chosen_lon = map_data["last_clicked"]["lng"]
            except Exception:
                chosen_lat = None
                chosen_lon = None

            # 2) Fallback: use geocoded coords we computed earlier (or re-run geocode if needed)
            if chosen_lat is None:
                # attempt geocode again robustly (in case earlier attempt was skipped)
                try:
                    loc = geolocator.geocode(location_name.strip() + ", India")
                    if loc:
                        chosen_lat = loc.latitude
                        chosen_lon = loc.longitude
                except Exception:
                    # last resort: try without appending country
                    try:
                        loc = geolocator.geocode(location_name.strip())
                        if loc:
                            chosen_lat = loc.latitude
                            chosen_lon = loc.longitude
                    except Exception:
                        chosen_lat = None
                        chosen_lon = None

            if chosen_lat is None or chosen_lon is None:
                st.error("⚠ Could not determine exact location. Please click a point on the map or provide a more specific address.")
            else:
                try:
                    unique_id = f"{phone}{int(time.time()*1_000_000)}{uuid4().hex[:6]}"

                    new_donation = {
                        "id": unique_id,
                        "donor": st.session_state.user["name"],
                        "phone": phone,
                        "food": food_item,
                        "quantity": quantity_text,
                        "availability": availability,
                        "location": location_name,
                        "lat": float(chosen_lat),
                        "lon": float(chosen_lon),
                        "status": "active",
                        "collector_name": None,
                        "collector_phone": None,
                        "created_at": now_ts(),
                        "accepted_at": None,
                        "picked_up_at": None,
                        "cancelled_at": None,
                    }

                    st.session_state.donations.append(new_donation)
                    update_donations()
                    st.success("🎉 Donation saved successfully with exact location!")
                    st.rerun()
                except Exception:
                    st.error("⚠ Error saving donation. Please try again.")

    # ---------------------------
    # DONATION HISTORY
    # ---------------------------
    st.write("---")
    with st.expander("📜 Donation History (All)"):
        if my_donations:
            for d in sorted(my_donations, key=lambda x: x.get("created_at") or 0, reverse=True):
                status = d.get("status", "active")
                accepted_at = fmt_time(d.get("accepted_at"))
                picked_at = fmt_time(d.get("picked_up_at"))
                cancelled_at = fmt_time(d.get("cancelled_at"))
                st.markdown(
                    f"- **{d.get('food','?')}** • **{d.get('quantity','?')}** • 📍 {d.get('location','?')} • "
                    f"🕒 Created: {fmt_time(d.get('created_at'))} • "
                    f"🏷 Status: `{status}`"
                    + (f" • 🤝 Accepted: {accepted_at}" if d.get("accepted_at") else "")
                    + (f" • ✅ Picked Up: {picked_at}" if d.get("picked_up_at") else "")
                    + (f" • ❌ Cancelled: {cancelled_at}" if d.get("cancelled_at") else "")
                )
        else:
            st.info("No donation history yet.")

    if st.button("⬅ Back"):
        st.session_state.page = "role_select"
        st.rerun()

# -----------------------------------------------------------------------------
# COLLECTOR PAGE
# -----------------------------------------------------------------------------
def collector_page():
    st.header("🚚 Collector Dashboard")
    st.write("Enter your location to see nearby donors and navigate easily:")

    with st.form("collector_location_form"):
        collector_location = st.text_input("📍 Enter Your Location (required, e.g., 'Indiranagar, Bangalore')")
        submitted = st.form_submit_button("Set My Location")

    if submitted:
        if not collector_location:
            st.error("⚠ Please enter your location.")
        else:
            try:
                loc = geolocator.geocode(collector_location)
                if loc:
                    st.session_state.collector_coords = (loc.latitude, loc.longitude, collector_location)
                    st.success(f"📍 Location set to: {collector_location}")
                else:
                    st.error("⚠ Could not find that location. Please try a more specific address.")
            except Exception:
                st.error("⚠ Error processing location. Please try again.")

    # Map center
    if st.session_state.collector_coords:
        map_center = [st.session_state.collector_coords[0], st.session_state.collector_coords[1]]
    else:
        map_center = [12.9716, 77.5946]  # default center (Bangalore)

    m = folium.Map(location=map_center, zoom_start=12)

    # Filter donors: show 'active', plus 'accepted by me'
    nearby_donors = []
    all_donations = st.session_state.donations
    me_phone = st.session_state.user["phone"]

    if st.session_state.collector_coords:
        clat, clon, cname = st.session_state.collector_coords
        folium.Marker(
            [clat, clon],
            popup=f"🧍 Collector: {cname}",
            icon=folium.Icon(color="blue", icon="user")
        ).add_to(m)

        for d in all_donations:
            if d.get("lat") is None or d.get("lon") is None:
                continue
            status = d.get("status", "active")
            if status not in ("active", "accepted"):
                continue
            if status == "accepted" and d.get("collector_phone") != me_phone:
                continue

            d_coords = (d["lat"], d["lon"])
            dist = geodesic((clat, clon), d_coords).km
            if dist <= NEARBY_RADIUS_KM:
                d_copy = {**d}
                d_copy["distance_km"] = round(dist, 2)
                nearby_donors.append(d_copy)
    else:
        for d in all_donations:
            status = d.get("status", "active")
            if status == "active" or (status == "accepted" and d.get("collector_phone") == me_phone):
                if d.get("lat") and d.get("lon"):
                    nearby_donors.append({**d})

    # donor markers
    for d in nearby_donors:
        link = gmaps_dir_link(d["lat"], d["lon"])
        status = d.get("status", "active")
        extra = f" • Status: {status}"
        if status == "accepted":
            cname = d.get("collector_name") or "Collector"
            extra += f" (by {cname})"
        popup_html = (
            f"<b>{d.get('food','?')}</b> • <b>{d.get('quantity','?')}</b> by {d.get('donor','?')}"
            f"<br>📍 {d.get('location','?')}"
            f"<br>⏳ {d.get('availability','?')}"
            f"<br>📞 {d.get('phone','?')}"
            f"<br>{extra}"
        )
        if "distance_km" in d:
            popup_html += f"<br>📏 {d['distance_km']} km away"
        popup_html += f"<br><a href='{link}' target='_blank'>➡ Directions</a>"

        folium.Marker(
            [d["lat"], d["lon"]],
            popup=popup_html,
            tooltip=f"{d.get('food','?')} ({d.get('quantity','?')}) by {d.get('donor','?')}",
            icon=folium.Icon(color="green", icon="cutlery", prefix="fa"),
        ).add_to(m)

    st_folium(m, height=500, width=800)

    # --- Browse & Accept / Confirm / Cancel Acceptance ---
    st.subheader("📋 Browse Donors")
    active_labels = []
    active_map = []
    for d in nearby_donors:
        status = d.get("status", "active")
        if status == "active" or (status == "accepted" and d.get("collector_phone") == me_phone):
            label = f"{d.get('food','?')} • {d.get('quantity','?')} • {d.get('donor','?')}"
            if "distance_km" in d:
                label += f" • {d['distance_km']} km"
            label += f" • Status: {status}"
            active_labels.append(label)
            active_map.append(d)

    if active_labels:
        selected_label = st.selectbox("Select a donor to view details:", active_labels)
        chosen = active_map[active_labels.index(selected_label)]

        link = gmaps_dir_link(chosen["lat"], chosen["lon"])
        status = chosen.get("status", "active")
        status_line = f"- 🏷 Status: {status}"
        if status == "accepted":
            cname = chosen.get("collector_name") or "Collector"
            cphone = chosen.get("collector_phone") or "N/A"
            status_line += f" (by {cname}, 📞 {cphone})"

        st.success(
            f"👤 Donor: {chosen.get('donor','?')}\n\n"
            f"- 🍲 Food: {chosen.get('food','?')}\n"
            f"- 📦 Quantity: {chosen.get('quantity','?')}\n"
            f"- 📞 Phone: {chosen.get('phone','?')}\n"
            f"- 📍 Location: {chosen.get('location','?')}\n"
            f"- ⏳ Available Until: {chosen.get('availability','?')}\n"
            + (f"- 📏 Distance: {chosen.get('distance_km','?')} km\n" if 'distance_km' in chosen else "")
            + f"{status_line}\n"
            + f"- 🗺 Directions: [Open in Google Maps]({link})"
        )

        colA, colB, colC = st.columns(3)
        if status == "active":
            with colA:
                if st.button("🤝 Accept Request", key=f"accept_{chosen['id']}"):
                    # Accept the donation (assign to me)
                    for dd in st.session_state.donations:
                        if dd["id"] == chosen["id"] and dd.get("status") == "active":
                            dd["status"] = "accepted"
                            dd["collector_name"] = st.session_state.user["name"]
                            dd["collector_phone"] = me_phone
                            dd.setdefault("created_at", now_ts())
                            dd["accepted_at"] = now_ts()
                    update_donations()
                    st.success("✅ Request accepted! Donor will see a notification.")
                    st.rerun()
        elif status == "accepted" and chosen.get("collector_phone") == me_phone:
            with colB:
                if st.button("✅ Confirm Pickup", key=f"pickup_{chosen['id']}"):
                    for dd in st.session_state.donations:
                        if dd["id"] == chosen["id"] and dd.get("status") == "accepted" and dd.get("collector_phone") == me_phone:
                            dd["status"] = "picked_up"
                            dd["picked_up_at"] = now_ts()
                    update_donations()
                    st.success("🎉 Pickup confirmed! The donor will see a pickup notification.")
                    st.rerun()
            with colC:
                if st.button("❌ Cancel Acceptance", key=f"cancel_accept_{chosen['id']}"):
                    # Revert to active, clear assignment + accepted_at
                    for dd in st.session_state.donations:
                        if dd["id"] == chosen["id"] and dd.get("status") == "accepted" and dd.get("collector_phone") == me_phone:
                            dd["status"] = "active"
                            dd["collector_name"] = None
                            dd["collector_phone"] = None
                            dd["accepted_at"] = None
                    update_donations()
                    st.info("↩ Acceptance cancelled. Donation is visible to other collectors again.")
                    st.rerun()
        else:
            st.info("This donation is accepted by another collector.")

    else:
        st.info("No donations you can act on within 10 km of your set location.")

    # --- Collection History for this Collector ---
    st.write("---")
    with st.expander("📜 My Collection History"):
        accepted_by_me = [
            d for d in st.session_state.donations
            if d.get("status") == "accepted" and d.get("collector_phone") == me_phone
        ]
        picked_by_me = [
            d for d in st.session_state.donations
            if d.get("status") == "picked_up" and d.get("collector_phone") == me_phone
        ]

        st.markdown("**🤝 Accepted by Me (Pending Pickup)**")
        if accepted_by_me:
            for d in sorted(accepted_by_me, key=lambda x: x.get("accepted_at") or 0, reverse=True):
                row = st.container()
                with row:
                    st.info(
                        f"- 🍲 {d.get('food','?')} • {d.get('quantity','?')} • 👤 Donor: {d.get('donor','?')} • "
                        f"📍 {d.get('location','?')} • 🕒 Accepted: {fmt_time(d.get('accepted_at'))}"
                    )
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("✅ Confirm Pickup", key=f"hist_pickup_{d['id']}"):
                            for dd in st.session_state.donations:
                                if dd["id"] == d["id"] and dd.get("status") == "accepted" and dd.get("collector_phone") == me_phone:
                                    dd["status"] = "picked_up"
                                    dd["picked_up_at"] = now_ts()
                            update_donations()
                            st.success("🎉 Pickup confirmed!")
                            st.rerun()
                    with c2:
                        if st.button("❌ Cancel Acceptance", key=f"hist_cancel_accept_{d['id']}"):
                            for dd in st.session_state.donations:
                                if dd["id"] == d["id"] and dd.get("status") == "accepted" and dd.get("collector_phone") == me_phone:
                                    dd["status"] = "active"
                                    dd["collector_name"] = None
                                    dd["collector_phone"] = None
                                    dd["accepted_at"] = None
                            update_donations()
                            st.info("↩ Acceptance cancelled.")
                            st.rerun()
        else:
            st.write("No pending pickups accepted by you.")

        st.markdown("**✅ Picked Up by Me**")
        if picked_by_me:
            for d in sorted(picked_by_me, key=lambda x: x.get("picked_up_at") or 0, reverse=True):
                st.success(
                    f"- 🍲 {d.get('food','?')} • {d.get('quantity','?')} • 👤 Donor: {d.get('donor','?')} • "
                    f"📍 {d.get('location','?')} • 🕒 Picked Up: {fmt_time(d.get('picked_up_at'))}"
                )
        else:
            st.write("No completed pickups yet.")

    if st.button("⬅ Back"):
        st.session_state.page = "role_select"
        st.rerun()

# -----------------------------------------------------------------------------
# COMMUNITY PAGE (placeholder)
# -----------------------------------------------------------------------------
def community_page():
    st.header("🤝 Community Dashboard")
    st.write("Here the community can view resources and events.")
    if st.button("⬅ Back"):
        st.session_state.page = "role_select"
        st.rerun()

# -----------------------------------------------------------------------------
# ROUTER
# -----------------------------------------------------------------------------
def main_router():
    page = st.session_state.page
    if page == "login":
        login_page()
    elif page == "role_select":
        role_select_page()
    elif page == "donor_page":
        donor_page()
    elif page == "collector_page":
        collector_page()
    elif page == "community_page":
        community_page()
    else:
        st.session_state.page = "login"
        st.rerun()

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main_router()
