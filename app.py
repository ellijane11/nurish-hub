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
from PIL import Image
import glob

def generate_form_key(prefix, donor):
    return f"{prefix}{donor['phone']}{donor.get('name', '')}_{donor.get('id', '')}"
def generate_form_key(prefix, user):
    return f"{prefix}{user['phone']}{user.get('name', '')}_{user.get('id', '')}"

# APP CONFIG
st.set_page_config(page_title="Food Donation App", page_icon="🍲", layout="centered")
# CONSTANTS / FILES
DATA_FILE = "donations.json"
USERS_FILE = "users.json"
FEEDBACK_FILE = "feedback.json"   # <--- new file
REPORTS_FILE = "reports.json"
BLOCKED_FILE = "blocked_users.json"

NEARBY_RADIUS_KM = 10  # show donors within this radius of the collector (km)
# Notification event labels
DONOR_EVENTS = ("accepted", "picked_up", "cancelled")
COLLECTOR_EVENTS = ("assigned", "unassigned")  # future-reserved (not shown now)
FEEDBACK_MIN_LEN = 0
FEEDBACK_MAX_LEN = 2000
FEEDBACK_ALLOWED_RATINGS = [1, 2, 3, 4, 5]
# HELPERS: TIME, HASH, LINKS
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
def short_id(prefix: str = "") -> str:
    """Generate a short-ish unique id with optional prefix."""
    return f"{prefix}{int(time.time()*1_000)}_{uuid4().hex[:8]}"
def sanitize_feedback_text(text: str) -> str:
    """
    Simple normalization for feedback text:
      - strip leading/trailing whitespace
      - collapse long runs of whitespace
      - cap maximum length
    """
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:FEEDBACK_MAX_LEN]
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

def load_feedback() -> List[Dict[str, Any]]:
    """
    Load feedback entries with structure (list of dicts):
    {
        "id": "fb_...",
        "role": "donor" | "collector",
        "user_phone": "##########",
        "user_name": "Alice",
        "anonymous": bool,
        "rating": int | None,
        "text": "message",
        "created_at": int (ts),
        "context": {
            "donation_id": "...",     # optional
            "status_snapshot": "...",  # optional
        }
    }
    """
    feedback = load_json(FEEDBACK_FILE, [])
    changed = False

    # normalize legacy or malformed entries
    normed: List[Dict[str, Any]] = []
    for entry in feedback:
        e = dict(entry) if isinstance(entry, dict) else {}
        if not e.get("id"):
            e["id"] = short_id("fb_")
            changed = True
        # role normalization
        role = e.get("role")
        if role not in ("donor", "collector"):
            # best effort: default unknown role to "donor"
            e["role"] = "donor"
            changed = True
        # rating normalization
        if e.get("rating") not in FEEDBACK_ALLOWED_RATINGS:
            # allow None for unrated
            if e.get("rating") is None:
                pass
            else:
                e["rating"] = None
                changed = True
        # text normalization
        e["text"] = sanitize_feedback_text(e.get("text", ""))

        # context normalization
        ctx = e.get("context")
        if not isinstance(ctx, dict):
            e["context"] = {}
            changed = True

        # anonymous boolean
        e["anonymous"] = bool(e.get("anonymous", False))

        # created_at
        if not e.get("created_at"):
            e["created_at"] = now_ts()
            changed = True

        normed.append(e)

    if changed:
        save_json(FEEDBACK_FILE, normed)
        return normed
    return feedback


def save_feedback(feedback_list: List[Dict[str, Any]]):
    """Persist all feedback entries."""
    save_json(FEEDBACK_FILE, feedback_list)
def load_reports() -> List[Dict[str, Any]]:
    return load_json(REPORTS_FILE, [])

def save_reports(reports: List[Dict[str, Any]]):
    save_json(REPORTS_FILE, reports)

def load_blocked_users() -> List[str]:
    return load_json(BLOCKED_FILE, [])

def save_blocked_users(blocked: List[str]):
    save_json(BLOCKED_FILE, blocked)

def is_blocked(phone: str) -> bool:
    return phone in load_blocked_users()

def block_user(phone: str):
    blocked = load_blocked_users()
    if phone not in blocked:
        blocked.append(phone)
        save_blocked_users(blocked)

# SESSION STATE INIT                                                           #
if "donations" not in st.session_state:
    st.session_state.donations = load_donations()
if "users" not in st.session_state:
    st.session_state.users = load_users()
if "feedback" not in st.session_state:
    st.session_state.feedback = load_feedback()
if "page" not in st.session_state:
    st.session_state.page = "login"
if "user" not in st.session_state:
    st.session_state.user = None  # {"name":..., "phone":..., "email":...}
if "collector_coords" not in st.session_state:
    st.session_state.collector_coords = None  # (lat, lon, label)
if "reports" not in st.session_state:
    st.session_state.reports = load_reports()
if "blocked_users" not in st.session_state:
    st.session_state.blocked_users = load_blocked_users()

# GLOBALS & UPDATE SHORTCUTS                                                   #
geolocator = Nominatim(user_agent="food_is_hope")
def update_donations():
    save_donations(st.session_state.donations)
def update_users():
    save_users(st.session_state.users)
def update_feedback():
    save_feedback(st.session_state.feedback)
def update_reports():
    save_reports(st.session_state.reports)

def update_blocked_users():
    save_blocked_users(st.session_state.blocked_users)

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
# FEEDBACK HELPERS                                                             #
def build_feedback_entry(
    role: str,
    user_phone: str,
    user_name: str,
    text: str,
    rating: Optional[int],
    anonymous: bool,
    donation_id: Optional[str] = None,
    status_snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construct a normalized feedback entry dict.
    The platform does not reply to feedback (non-intrusive logging only).
    """
    text = sanitize_feedback_text(text)
    rating_norm = rating if rating in FEEDBACK_ALLOWED_RATINGS else None

    entry = {
        "id": short_id("fb_"),
        "role": "collector" if role == "collector" else "donor",
        "user_phone": str(user_phone or "").strip(),
        "user_name": (user_name or "").strip(),
        "anonymous": bool(anonymous),
        "rating": rating_norm,
        "text": text,
        "created_at": now_ts(),
        "context": {},
    }
    if donation_id:
        entry["context"]["donation_id"] = donation_id
    if status_snapshot:
        entry["context"]["status_snapshot"] = status_snapshot
    return entry


def append_feedback(entry: Dict[str, Any]) -> None:
    """Append one feedback entry to session and persist."""
    st.session_state.feedback.append(entry)
    update_feedback()


def my_feedback_history(role: str, my_phone: str) -> List[Dict[str, Any]]:
    """Return feedback authored by the current user for a given role."""
    out = []
    for f in st.session_state.feedback:
        if f.get("role") == role and f.get("user_phone") == my_phone:
            out.append(f)
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out


def community_feedback_recent(limit: int = 25) -> List[Dict[str, Any]]:
    """
    Return most recent feedback entries across roles.
    The display anonymizes (respects the 'anonymous' flag) and excludes
    sensitive details (we only show role, rating, excerpt, and time).
    """
    items = list(st.session_state.feedback)
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return items[:limit]


def feedback_role_badge(role: str) -> str:
    """Pretty label for role."""
    return "🍎 Donor" if role == "donor" else "🚚 Collector"


def feedback_rating_stars(rating: Optional[int]) -> str:
    """Return a simple star string for rating (1–5), or '—' if missing."""
    if rating in FEEDBACK_ALLOWED_RATINGS:
        return "★" * rating + "☆" * (5 - rating)
    return "—"


def feedback_excerpt(text: str, width: int = 160) -> str:
    """Short excerpt for community wall."""
    t = (text or "").strip()
    if len(t) <= width:
        return t
    return t[: width - 1] + "…"        

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
        if is_blocked(phone):
            st.error("🚫 This account has been blocked due to safety concerns.")
            return

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
    # Admin Panel Access (only for admin)
    if st.session_state.user["phone"] == "8891867973":
      if st.button("🛡 Admin Panel"):
        st.session_state.page = "admin_panel"
        st.rerun()


    if st.button("Logout"):
        st.session_state.page = "login"
        st.session_state.user = None
        st.session_state.collector_coords = None
        st.rerun()
# -----------------------------------------------------------------------------#
# SHARED FEEDBACK UI (used inside donor & collector pages)                     #
# -----------------------------------------------------------------------------#
def feedback_widget(role: str, possible_donation_id: Optional[str] = None, status_snapshot: Optional[str] = None):
    """
    Render a compact feedback form for the given role, to be embedded in
    donor_page() and collector_page(). The platform does not reply to feedback.
    """
    user = st.session_state.user
    my_phone = user["phone"]
    my_name = user["name"]
    block_key = f"feedback_block_{role}_{possible_donation_id or 'none'}"

    st.write("---")
    with st.expander("📝 Share Feedback (optional)"):
     st.caption("We appreciate your thoughts. This is anonymous if you choose, and we don’t reply individually.")

     with st.form(f"form_feedback_{block_key}"):
        txt = st.text_area(
            "Your feedback",
            help="Share anything about your experience. (Min 5 characters)",
            key=f"txt_feedback_{block_key}",
            height=120,
        )
        colA, colB = st.columns(2)
        with colA:
            rating = st.selectbox(
                "Rating (optional)",
                options=["— (no rating)"] + [str(r) for r in FEEDBACK_ALLOWED_RATINGS],
                index=0,
                key=f"rating_select_{block_key}",
                help="Leave as '—' if you prefer not to rate.",
            )
        with colB:
            anonymous = st.checkbox(
                "Submit anonymously",
                value=False,
                key=f"chk_anonymous_{block_key}",
                help="If checked, your name and phone will not be shown on the community wall.",
            )

        submit_label = "Submit Feedback"
        submitted = st.form_submit_button(submit_label, use_container_width=True)

    if submitted:
        normalized_text = sanitize_feedback_text(txt)
        if len(normalized_text) < FEEDBACK_MIN_LEN:
            st.error(f"Please enter at least {FEEDBACK_MIN_LEN} characters of feedback.")
            return

        rating_val: Optional[int] = None
        if rating != "— (no rating)":
            try:
                rating_val_int = int(rating)
                if rating_val_int in FEEDBACK_ALLOWED_RATINGS:
                    rating_val = rating_val_int
            except Exception:
                rating_val = None

        entry = build_feedback_entry(
            role=role,
            user_phone=my_phone,
            user_name=my_name,
            text=normalized_text,
            rating=rating_val,
            anonymous=anonymous,
            donation_id=possible_donation_id,
            status_snapshot=status_snapshot,
        )
        append_feedback(entry)
        st.success("✅ Feedback submitted. Thank you!")
        st.rerun()

    # Your feedback history (for this role)
    history = my_feedback_history(role, my_phone)
    with st.expander("📜 My Feedback History (this role)"):
        if not history:
            st.info("You haven’t submitted any feedback yet for this role.")
        else:
            for i, f in enumerate(history, start=1):
                st.markdown(
                    f"- {fmt_time(f.get('created_at'))} • "
                    f"Role: {feedback_role_badge(f.get('role','donor'))} • "
                    f"Rating: {feedback_rating_stars(f.get('rating'))}\n\n"
                    f"  {f.get('text','')}"
                )
# -----------------------------------------------------------------------------
# DONOR PAGE
# -----------------------------------------------------------------------------
def donor_page():
    st.header("🍎 Donor Dashboard")
    st.markdown("""
✨ Pack the food with love — clean, sealed, and ready to share.<br>
📅 Add a note with the date and time it was prepared, if you can.<br>
🍲 Share only fresh, hygienic meals to spread health and happiness.
""", unsafe_allow_html=True)
    # -------------------------------------------------------------------------
    # Load Blocked Users and Filter Visible Donations
    # -------------------------------------------------------------------------
    blocked = load_blocked_users()
    visible_donations = [d for d in st.session_state.donations if d["phone"] not in blocked]

    phone = st.session_state.user["phone"]
    my_donations = [d for d in visible_donations if d.get("phone") == phone]

    accepted = [d for d in my_donations if d.get("status") == "accepted"]
    picked_up = [d for d in my_donations if d.get("status") == "picked_up"]
    active_donations = [d for d in my_donations if d.get("status", "active") == "active"]
    cancelled = [d for d in my_donations if d.get("status") == "cancelled"]

    # -------------------------------------------------------------------------
    # Notifications — ACCEPTED
    # -------------------------------------------------------------------------
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
                    f"Your donation {d.get('food','?')} ({d.get('quantity','?')}) at {d.get('location','?')} "
                    f"was accepted by {cname} (📞 {cphone}) at {when}."
                )
                if st.button("Mark as seen", key=seen_key):
                    mark_seen(phone, "donor", did, "accepted")
                    st.rerun()
            else:
                with st.expander(f"Seen: {d.get('food','?')} accepted by {cname} at {when}"):
                    st.write("You have marked this notification as seen.")
                    if st.button("Unhide (show again)", key=f"unsee_accept_{did}_{idx}"):
                        users = st.session_state.users
                        users[phone]["seen"]["donor"].setdefault(did, {})
                        users[phone]["seen"]["donor"][did].pop("accepted", None)
                        update_users()
                        st.rerun()

    # -------------------------------------------------------------------------
    # Notifications — PICKED UP
    # -------------------------------------------------------------------------
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
                    f"Your donation {d.get('food','?')} ({d.get('quantity','?')}) at {d.get('location','?')} "
                    f"was picked up by {cname} at {when}!"
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

    # -------------------------------------------------------------------------
    # ACTIVE DONATIONS
    # -------------------------------------------------------------------------
    if active_donations:
        st.subheader("Your Active Donations")
        for idx, d in enumerate(sorted(active_donations, key=lambda x: x.get("created_at") or 0, reverse=True)):
            st.info(
                f"🍲 {d.get('food','?')} • {d.get('quantity','?')} | 📍 {d.get('location','?')} | ⏳ {d.get('availability','?')} | "
                f"🕒 Created: {fmt_time(d.get('created_at'))}"
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button(
                    f"❌ Cancel '{d.get('food','item')}'",
                    key=f"cancel_{d.get('id','noid')}_{idx}"
                ):
                    for dd in st.session_state.donations:
                        if dd["id"] == d["id"]:
                            dd["status"] = "cancelled"
                            dd["cancelled_at"] = now_ts()
                    update_donations()
                    st.rerun()
            with col2:
                st.write("")

    # -------------------------------------------------------------------------
    # CANCELLED DONATIONS
    # -------------------------------------------------------------------------
    if cancelled:
        st.subheader("🗂 Cancelled Donations")
        for idx, d in enumerate(sorted(cancelled, key=lambda x: x.get("cancelled_at") or x.get("created_at") or 0, reverse=True)):
            when = fmt_time(d.get("cancelled_at"))
            did = d["id"]
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

    # -------------------------------------------------------------------------
    # ADD NEW DONATION
    # -------------------------------------------------------------------------
    st.write("---")
    st.subheader("Add a New Donation")

    with st.form("donor_form_main"):
        food_item = st.text_input("🍲 Food Item")
        quantity_text = st.text_input("📦 Quantity (e.g. '10 meals', '5 kg rice', '20 boxes')")
        availability = st.text_input("📅 Available Until date and time (e.g. '9 PM,22/8/25')")
        location_name = st.text_input("📍 Enter Your Location (required, e.g., 'MG Road, Bangalore')")

        st.markdown("📍 Recommended: First type a specific address/landmark; the map will center there. Then click the exact pickup spot on the map to fine-tune.")

        default_center = [12.9716, 77.5946]
        map_center = default_center
        geocoded_lat = None
        geocoded_lon = None

        try:
            if location_name and location_name.strip():
                safe_query = location_name.strip()
                try:
                    geocoded = geolocator.geocode(safe_query + ", India")
                except Exception:
                    geocoded = None
                    try:
                        geocoded = geolocator.geocode(safe_query)
                    except Exception:
                        geocoded = None

                if geocoded:
                    geocoded_lat = geocoded.latitude
                    geocoded_lon = geocoded.longitude
                    map_center = [geocoded_lat, geocoded_lon]
        except Exception:
            map_center = default_center

        zoom_level = 14 if map_center != default_center else 5
        m = folium.Map(location=map_center, zoom_start=zoom_level)

        if geocoded_lat and geocoded_lon:
            folium.Marker([geocoded_lat, geocoded_lon], tooltip="Suggested location").add_to(m)

        map_data = st_folium(m, height=380, width=700)
        submitted = st.form_submit_button("Save Donation")

    if submitted:
        if not (food_item and quantity_text and availability and location_name):
            st.error("⚠ Please fill all required fields.")
        else:
            chosen_lat, chosen_lon = None, None
            try:
                if map_data and map_data.get("last_clicked"):
                    chosen_lat = map_data["last_clicked"]["lat"]
                    chosen_lon = map_data["last_clicked"]["lng"]
            except Exception:
                pass

            if chosen_lat is None:
                try:
                    loc = geolocator.geocode(location_name.strip() + ", India")
                    if loc:
                        chosen_lat, chosen_lon = loc.latitude, loc.longitude
                except Exception:
                    pass

            if chosen_lat is None or chosen_lon is None:
                st.error("⚠ Could not determine exact location.")
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
                    st.success("🎉 Donation saved successfully!")
                    st.rerun()
                except Exception:
                    st.error("⚠ Error saving donation.")

    # -------------------------------------------------------------------------
    # DONATION HISTORY
    # -------------------------------------------------------------------------
    st.write("---")
    with st.expander("📜 Donation History (All)"):
        if my_donations:
            for d in sorted(my_donations, key=lambda x: x.get("created_at") or 0, reverse=True):
                status = d.get("status", "active")
                accepted_at = fmt_time(d.get("accepted_at"))
                picked_at = fmt_time(d.get("picked_up_at"))
                cancelled_at = fmt_time(d.get("cancelled_at"))
                st.markdown(
                    f"- {d.get('food','?')} • {d.get('quantity','?')} • 📍 {d.get('location','?')} • "
                    f"🕒 Created: {fmt_time(d.get('created_at'))} • "
                    f"🏷 Status: {status}"
                    + (f" • 🤝 Accepted: {accepted_at}" if d.get("accepted_at") else "")
                    + (f" • ✅ Picked Up: {picked_at}" if d.get("picked_up_at") else "")
                    + (f" • ❌ Cancelled: {cancelled_at}" if d.get("cancelled_at") else "")
                )
        else:
            st.info("No donation history yet.")

    # -------------------------------------------------------------------------
    # FEEDBACK
    # -------------------------------------------------------------------------
    feedback_widget(role="donor")

    # -------------------------------------------------------------------------
    # REPORT A COLLECTOR (Moved to Bottom)
    # -------------------------------------------------------------------------
    st.write("---")
    st.markdown("### 🚨 Report a Collector")

    interacted_collectors = [d for d in st.session_state.get("interactions", []) if d["type"] == "collector"]

    if interacted_collectors:
        selected_collector = st.selectbox(
            "Select a collector to report",
            interacted_collectors,
            format_func=lambda c: f"{c['name']} ({c['phone']})"
        )

        form_key = f"report_form_{selected_collector['phone']}{selected_collector.get('name','')}{selected_collector.get('id','')}"
        with st.form(key=form_key):
            reason = st.text_input("Reason for report")
            comment = st.text_area("Additional comments")
            if st.form_submit_button("Submit Report"):
                new_report = {
                    "id": short_id("rep_"),
                    "reported_phone": selected_collector["phone"],
                    "reporter_phone": st.session_state.user["phone"],
                    "reason": reason,
                    "comment": comment,
                    "created_at": now_ts(),
                    "status": "pending"
                }
                reports = load_reports()
                reports.append(new_report)
                save_reports(reports)
                st.success(f"✅ Report submitted for {selected_collector['name']}")
    else:
        st.info("No past collector interactions found to report.")

    if st.button("⬅ Back", key="btn_back_donor"):
        st.session_state.page = "role_select"
        st.rerun()

# -----------------------------------------------------------------------------
# COLLECTOR PAGE
# -----------------------------------------------------------------------------
def collector_page():
    st.header("🚚 Collector Dashboard")
    
    st.markdown("""
🔍 Verify food is properly packed before pickup..<br>
🚴 Deliver with care and speed so every meal stays fresh and tasty.
""", unsafe_allow_html=True)
    

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

      # 🔔 Add this block right after the donor details
        with st.expander("🚨 Report This User"):
          with st.form(f"report_form_{chosen['phone']}"):
           reason = st.text_input("Reason for report")
           comment = st.text_area("Additional comments")
           if st.form_submit_button("Submit Report"):
              new_report = {
                "id": short_id("rep_"),
                "reported_phone": chosen["phone"],
                "reporter_phone": st.session_state.user["phone"],
                "reason": reason,
                "comment": comment,
                "created_at": now_ts(),
                "status": "pending"
              }
              reports = load_reports()
              reports.append(new_report)
              save_reports(reports)
              st.success("✅ Report submitted.")
 

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

        st.markdown("🤝 Accepted by Me (Pending Pickup)")
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

        st.markdown("✅ Picked Up by Me**")
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
# ---------------------------
    # FEEDBACK (COLLECTOR)
    # ---------------------------
    
    feedback_widget(role="collector")

    if st.button("⬅ Back", key="btn_back_collector"):
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

    # Ensure image directory exists
    save_dir = "community_images"
    os.makedirs(save_dir, exist_ok=True)

    # Upload section
    st.subheader("📸 Share a Photo")
    uploaded_image = st.file_uploader("Upload an image", type=["png", "jpg", "jpeg"])
    caption = st.text_input("📝 Add a caption for your photo")

    metadata_path = "image_metadata.json"

    if uploaded_image is not None and caption:
        image = Image.open(uploaded_image)
        save_path = os.path.join(save_dir, uploaded_image.name)
        image.save(save_path)

        # Load existing metadata or initialize
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r") as f:
                    metadata = json.load(f)
            except json.JSONDecodeError:
                metadata = {}
        else:
            metadata = {}

        # Save new entry
        metadata[uploaded_image.name] = caption
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        st.success("✅ Image and caption uploaded successfully!")
        st.image(image, caption=caption, use_container_width=True)

    # Display gallery
    # Display gallery
    st.subheader("🌟 Community Gallery")
    metadata_path = "image_metadata.json"
    if os.path.exists(metadata_path):
      try:
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
      except json.JSONDecodeError:
            metadata = {}
      except (ValueError, FileNotFoundError):
            metadata = {}
    else:
        metadata = {}

    image_files = glob.glob(os.path.join(save_dir, "*"))
    for img_path in image_files:
        filename = os.path.basename(img_path)
        caption = metadata.get(filename, "No caption provided")
        st.image(img_path, caption=caption, use_container_width=True)




# ---------------- Admin Panel ----------------

def admin_panel():
    st.header("🛡 Admin Panel — Review Reports")
    reports = load_reports()

    for r in reports:
        if r["status"] == "pending":
            st.warning(f"📱 Reported User: {r['reported_phone']}")
            st.write(f"📝 Reason: {r['reason']}")
            st.write(f"💬 Comment: {r['comment']}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ Approve", key=f"approve_{r['id']}"):
                    r["status"] = "approved"
                    block_user(r["reported_phone"])
                    save_reports(reports)
                    st.success("User blocked.")
                    st.rerun()
            with col2:
                if st.button("❌ Reject", key=f"reject_{r['id']}"):
                    r["status"] = "rejected"
                    save_reports(reports)
                    st.info("Report rejected.")
                    st.rerun()

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
    #elif page == "my_feedback_page":
        #my_feedback_page()    
    elif page == "admin_panel":
        admin_panel()

    else:
        st.session_state.page = "login"
        st.rerun()

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main_router()




