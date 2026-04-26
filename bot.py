"""
Snaggit AI Bot — v3 (Multi-Inspector)
=====================================
All state in Supabase. Multiple inspectors via join code.
Zones defined upfront. AI defect classification.
PDF sent to ALL members in Telegram.

Tables (Supabase):
  inspections        — one per inspection, meta + join code
  inspection_zones   — one per zone, defects[], assigned_to, status
  inspection_members — who joined this inspection

Flow:
  Lead:   /start → New → meta fields → add zones → get code → start/wait
  Member: /start → Join → enter code → pick zone → inspect
  Both:   pick zone → photo → AI → confirm → next defect / finish zone
  Any:    /finish → if all zones done → PDF → send to all members
"""

import json, os, logging, subprocess, asyncio, re, random, string, textwrap
from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")
REPORT_DIR     = os.environ.get("REPORT_DIR", "/app/data")
ASSETS_DIR     = os.environ.get("ASSETS_DIR", REPORT_DIR)

# ── Supabase client ──────────────────────────────────────────────────────────
_SUPABASE = None
try:
    from supabase import create_client
    if SUPABASE_URL and SUPABASE_KEY:
        _SUPABASE = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase connected")
    else:
        logger.warning("SUPABASE_URL or SUPABASE_KEY not set")
except ImportError:
    logger.warning("supabase package not installed")

# ── Conversation states ──────────────────────────────────────────────────────
(
    START_MENU,                                           # 0
    # Meta fields (lead only)
    DATE, PROJECT_NAME, UNIT_NUMBER, PROPERTY_TYPE,       # 1-4
    CLIENT_NAME, CLIENT_EMAIL, REASON,                    # 5-7
    INSPECTOR, ADDRESS, DEVELOPER,                        # 8-10
    TOTAL_AREA, FLOOR_NUMBER, NUM_ROOMS, FURNISHED,       # 11-14
    YEAR_BUILT,                                           # 15
    # Zone setup (lead only)
    SETUP_ZONE_NAME, SETUP_ZONE_TYPE, SETUP_ZONES_DONE,  # 16-18
    # Join (member)
    JOIN_CODE,                                            # 19
    # Inspection
    PICK_ZONE,                                            # 20
    DEFECT_PHOTO, DEFECT_SEVERITY,                        # 21-22
    DEFECT_DESC, AFTER_DEFECT,                            # 23-24
    # Resume
    RESUME_MENU,                                          # 25
    # Edit defect
    EDIT_PICK_DEFECT, EDIT_DEFECT_SEV, EDIT_DEFECT_DESC,  # 26-28
) = range(29)

# ── Constants ────────────────────────────────────────────────────────────────
PROPERTY_TYPES = ["Apartment", "Villa", "Townhouse", "Penthouse", "Duplex", "Studio", "Office"]
FURNISHED_OPTIONS = ["Furnished", "Unfurnished", "Semi-furnished"]
SEVERITY_OPTIONS = ["compliant", "minor", "medium", "critical"]

MEP_CHECKLISTS = {
    "electrical": [
        "Appliances — functional check",
        "Power sockets — condition and load check",
        "DB panel — overheating and safety check",
        "Lights — operation and condition check",
    ],
    "hvac": [
        "Ceiling space — visual inspection",
        "Thermal camera — heat anomaly detection",
        "Exhaust fans — airflow and operation check",
    ],
    "plumbing": [
        "Sinks & taps — leakage and pressure check",
        "Drainage pipes & balcony drainage — flow test",
        "Shower drainage — blockage and flow check",
        "Water heaters — functionality and safety check",
        "Toilet flush — proper operation test",
    ],
    "moisture": [
        "Wall moisture — dampness and leakage detection",
    ],
}

# ── AI Prompts ───────────────────────────────────────────────────────────────
# (Per-defect AI classification removed in v4 — inspector sets severity + description manually.
#  AI is only used for PDF summary/observation texts in generate_report_ai_texts.)


# ══════════════════════════════════════════════════════════════════════════════
#  SUPABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sb():
    """Return Supabase client or raise."""
    if not _SUPABASE:
        raise RuntimeError("Supabase not connected")
    return _SUPABASE


def generate_join_code() -> str:
    """6-char uppercase alphanumeric code."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ── Inspection CRUD ──────────────────────────────────────────────────────────

def create_inspection(user_id: str, meta: dict) -> dict:
    """Create a new inspection row, return it."""
    code = generate_join_code()
    row = {
        "code": code,
        "status": "setup",
        "meta": meta,
        "created_by": user_id,
    }
    res = _sb().table("inspections").insert(row).execute()
    return res.data[0]


def get_inspection_by_code(code: str) -> dict | None:
    res = _sb().table("inspections").select("*").eq("code", code.upper().strip()).execute()
    return res.data[0] if res.data else None


def get_inspection_by_id(inspection_id: str) -> dict | None:
    res = _sb().table("inspections").select("*").eq("id", inspection_id).execute()
    return res.data[0] if res.data else None


def update_inspection(inspection_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    _sb().table("inspections").update(kwargs).eq("id", inspection_id).execute()


# ── Zones CRUD ───────────────────────────────────────────────────────────────

def add_zone(inspection_id: str, zone_number: int, name: str, zone_type: str = "regular") -> dict:
    row = {
        "inspection_id": inspection_id,
        "zone_number": zone_number,
        "name": name,
        "type": zone_type,
        "status": "pending",
        "defects": [],
    }
    res = _sb().table("inspection_zones").insert(row).execute()
    return res.data[0]


def get_zones(inspection_id: str) -> list:
    res = (_sb().table("inspection_zones")
           .select("*")
           .eq("inspection_id", inspection_id)
           .order("zone_number")
           .execute())
    return res.data


def get_zone_by_id(zone_id: str) -> dict | None:
    res = _sb().table("inspection_zones").select("*").eq("id", zone_id).execute()
    return res.data[0] if res.data else None


def update_zone(zone_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    _sb().table("inspection_zones").update(kwargs).eq("id", zone_id).execute()


def append_defect_to_zone(zone_id: str, defect: dict):
    """Append a defect to zone's defects JSONB array."""
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []
    defects.append(defect)
    update_zone(zone_id, defects=defects)


def delete_last_defect_from_zone(zone_id: str) -> bool:
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []
    if not defects:
        return False
    defects.pop()
    update_zone(zone_id, defects=defects)
    return True


def update_defect_in_zone(zone_id: str, defect_index: int, **kwargs) -> bool:
    """Update specific fields of a defect by its index in the zone's defects array."""
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []
    if defect_index < 0 or defect_index >= len(defects):
        return False
    for k, v in kwargs.items():
        defects[defect_index][k] = v
    update_zone(zone_id, defects=defects)
    return True


# ── Members CRUD ─────────────────────────────────────────────────────────────

def add_member(inspection_id: str, user_id: str, name: str = "", role: str = "inspector"):
    _sb().table("inspection_members").upsert({
        "inspection_id": inspection_id,
        "user_id": user_id,
        "name": name,
        "role": role,
    }).execute()


def get_members(inspection_id: str) -> list:
    res = _sb().table("inspection_members").select("*").eq("inspection_id", inspection_id).execute()
    return res.data


def get_user_active_inspection(user_id: str) -> dict | None:
    """Find active (non-complete) inspection for this user."""
    res = (_sb().table("inspection_members")
           .select("inspection_id")
           .eq("user_id", user_id)
           .execute())
    for m in res.data:
        insp = get_inspection_by_id(m["inspection_id"])
        if insp and insp["status"] != "complete":
            return insp
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  AI OBSERVATION TEXTS (for PDF only — NOT for per-defect classification)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_ai_texts(meta: dict, zones: list) -> dict:
    """Generate summary_obs, general_condition, urgent + per-zone 'obs' texts for the PDF.

    v4: single batched Anthropic call that returns all PDF narrative text.
    Per-defect AI classification was removed — inspectors set severity/description manually.
    This function only fills the narrative blocks in the PDF.
    """
    # Build per-zone defect listings for the prompt
    zone_blocks = []
    real_defects_count = 0  # excludes compliant
    compliant_count = 0
    for z in zones:
        z_defects = []
        for d in (z.get("defects") or []):
            sev = d.get("severity", "?")
            desc = d.get("description", "?")
            z_defects.append(f"  - {desc} ({sev})")
            if sev == "compliant":
                compliant_count += 1
            else:
                real_defects_count += 1
        is_mep = (z.get("type") == "mep")
        header = f"Zone #{z['zone_number']}: {z['name']}" + (" [MEP]" if is_mep else "")
        body = "\n".join(z_defects) if z_defects else "  (no defects recorded)"
        zone_blocks.append(f"{header}\n{body}")

    zones_text = "\n\n".join(zone_blocks) if zone_blocks else "No zones inspected."
    total_zones = len(zones)
    unit = meta.get("unit", "?")
    project = meta.get("project", "?")
    reason = meta.get("reason", "handover")

    # We need obs for EVERY zone keyed by zone_number so the PDF builder can look them up.
    zone_number_list = [str(z["zone_number"]) for z in zones]
    zone_obs_json_schema = ", ".join(f'"{zn}": "..."' for zn in zone_number_list) or '"1": "..."'

    prompt = textwrap.dedent(f"""\
    You are writing narrative text for a professional property snagging report for a Dubai property inspection.

    Project: {project}
    Unit: {unit}
    Reason: {reason}
    Total zones inspected: {total_zones}
    Total defects (excluding compliant): {real_defects_count}
    Compliant items (passed checks or positive observations, NOT defects): {compliant_count}

    Zones and their recorded items:
    {zones_text}

    Write professional, neutral English. No markdown, no bullets, plain text only. Do not invent defects, only describe what is listed.

    CRITICAL: When you mention "defects" or "comments" in any text, use the number {real_defects_count}, NOT {real_defects_count + compliant_count}. Compliant items are passed checks or positive observations — they are NOT defects, regardless of whether they're in a regular or MEP zone.

    Return ONLY raw JSON with this exact shape (no code fences):
    {{
      "summary_obs": "MAX 380 CHARACTERS. 2-3 short sentences for the Summary page. Overview of unit condition. Use defect count {real_defects_count}. Be concise as long text gets truncated.",
      "general_condition": "MAX 600 CHARACTERS. 2-3 sentences for the Conclusions page. Property condition, number of zones inspected, key areas of concern.",
      "urgent": "MAX 350 CHARACTERS. 1-2 short sentences listing the most critical/medium items that need immediate attention. If none, say 'No critical items identified.'",
      "zone_obs": {{ {zone_obs_json_schema} }}
    }}

    Rules for "zone_obs":
    - One entry per zone_number (keys must be strings matching exactly the zone numbers above).
    - Each value: 2-3 sentences, MAX 400 characters. Use real defect count for that zone (do NOT count compliant items as defects). For zones with no defects, write a positive statement (e.g. "Overall, the <Zone> is in good condition. No comments were noted during inspection.").
    - For ANY zone (regular or MEP), distinguish compliant items from real defects. Compliant items can appear in any zone. Example: "The Living Room has 2 defects identified, while 1 item was confirmed compliant." NOT "3 comments noted" if 1 is compliant.
    - For MEP zones specifically, phrase around systems tested. Example: "All electrical sockets were tested. 2 issues require attention."
    """)

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            if "content" not in data:
                logger.error(f"AI texts response missing 'content': {data}")
                return {}
            text = data["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text.strip())
            # Normalize zone_obs keys to strings just in case
            if "zone_obs" in parsed and isinstance(parsed["zone_obs"], dict):
                parsed["zone_obs"] = {str(k): v for k, v in parsed["zone_obs"].items()}
            return parsed
    except Exception as e:
        logger.error(f"AI texts failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  KEYBOARD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def inline_kb(options: list, prefix: str, columns: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=opt, callback_data=f"{prefix}:{opt}") for opt in options]
    rows = [buttons[i:i+columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)


def zone_picker_kb(zones: list, user_id: str) -> InlineKeyboardMarkup:
    """Build keyboard showing available zones. Done zones can be re-entered for editing."""
    buttons = []
    for z in zones:
        status = z["status"]
        assigned = z.get("assigned_to")
        name = z["name"]
        ztype = " ⚡" if z["type"] == "mep" else ""
        n_defects = len(z.get("defects") or [])

        if status == "done":
            extra = f" ({n_defects} defects)" if n_defects else ""
            label = f"✅ {z['zone_number']}. {name}{ztype}{extra}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:pick:{z['id']}")])
        elif assigned and assigned != user_id:
            label = f"🔒 {z['zone_number']}. {name}{ztype} (taken)"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:taken:{z['id']}")])
        else:
            extra = f" ({n_defects} defects)" if n_defects else ""
            label = f"📍 {z['zone_number']}. {name}{ztype}{extra}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"zone:pick:{z['id']}")])

    buttons.append([InlineKeyboardButton(text="🏁 Finish inspection", callback_data="zone:finish")])
    return InlineKeyboardMarkup(buttons)


# ══════════════════════════════════════════════════════════════════════════════
#  TEXT CLEANING (for PDF generation)
# ══════════════════════════════════════════════════════════════════════════════

def clean_unicode(text: str) -> str:
    """Replace problematic Unicode chars with ASCII equivalents."""
    if not text:
        return ""
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
        "\u00b2": "sq", "\u00b0": " deg", "\u2032": "'", "\u2033": '"',
        "\u200b": "", "\ufeff": "",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def trunc(text: str, max_len: int = 80) -> str:
    """Truncate and clean text for PDF."""
    text = clean_unicode(str(text or ""))
    text = text.replace("\n", " ").replace("\r", " ").strip()
    return text[:max_len] if len(text) > max_len else text


# ══════════════════════════════════════════════════════════════════════════════
#  BOT HANDLERS — START / NEW / JOIN
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point. Show start menu."""
    user_id = str(update.effective_user.id)
    context.user_data.clear()

    # Check for active inspection
    active = get_user_active_inspection(user_id) if _SUPABASE else None

    buttons = [
        [InlineKeyboardButton("🆕 New inspection", callback_data="start:new")],
        [InlineKeyboardButton("🔗 Join inspection", callback_data="start:join")],
    ]
    if active:
        buttons.insert(0, [InlineKeyboardButton(
            f"▶️ Resume: {active['meta'].get('project', '?')} — Unit {active['meta'].get('unit', '?')}",
            callback_data=f"start:resume:{active['id']}"
        )])

    await update.message.reply_text(
        "👋 <b>Snaggit AI — Property Inspection Bot</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return START_MENU


async def start_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle start menu selection."""
    query = update.callback_query
    await query.answer()
    data = query.data  # start:new / start:join / start:resume:<id>

    if data == "start:new":
        context.user_data["_meta"] = {}
        context.user_data["_role"] = "lead"
        await query.edit_message_text(
            "📅 <b>Step 1/15 — Date of Inspection</b>\n\nEnter date (e.g. 19.03.2026):",
            parse_mode="HTML",
        )
        return DATE

    elif data == "start:join":
        await query.edit_message_text(
            "🔗 <b>Join Inspection</b>\n\nEnter the 6-character join code:",
            parse_mode="HTML",
        )
        return JOIN_CODE

    elif data.startswith("start:resume:"):
        inspection_id = data.split(":", 2)[2]
        context.user_data["_inspection_id"] = inspection_id
        return await _show_zone_picker(query, context, inspection_id)

    return START_MENU


# ══════════════════════════════════════════════════════════════════════════════
#  META FIELD HANDLERS (lead only, 15 steps)
# ══════════════════════════════════════════════════════════════════════════════

META_FIELDS = [
    # (state, key, step_num, prompt, options_or_None)
    (DATE,          "date",      1,  "📅 Date of Inspection\n(e.g. 19.03.2026)", None),
    (PROJECT_NAME,  "project",   2,  "🏗 Project Name", None),
    (UNIT_NUMBER,   "unit",      3,  "🔢 Unit Number", None),
    (PROPERTY_TYPE, "type",      4,  "🏠 Property Type", PROPERTY_TYPES),
    (CLIENT_NAME,   "client",    5,  "👤 Client Name", None),
    (CLIENT_EMAIL,  "email",     6,  "📧 Client Email", None),
    (REASON,        "reason",    7,  "📋 Reason for Inspection", None),
    (INSPECTOR,     "inspector", 8,  "🧑‍🔧 Inspector Name", None),
    (ADDRESS,       "address",   9,  "📍 Property Address\n(e.g. Sobha Hartland, MBR City, Dubai)", None),
    (DEVELOPER,     "developer", 10, "🏢 Developer\n(e.g. Emaar, Sobha, Damac)", None),
    (TOTAL_AREA,    "area",      11, "📐 Total Area\n(e.g. 1200 sq ft)", None),
    (FLOOR_NUMBER,  "floor",     12, "🏢 Floor Number", None),
    (NUM_ROOMS,     "rooms",     13, "🛏 Number of Rooms\n(e.g. 1 Bedroom)", None),
    (FURNISHED,     "furnished", 14, "🛋 Furnished?", FURNISHED_OPTIONS),
    (YEAR_BUILT,    "year",      15, "📅 Year Built", None),
]

_META_BY_STATE = {state: i for i, (state, *_) in enumerate(META_FIELDS)}


def _next_meta_prompt(step_index: int) -> tuple:
    """Return (state, prompt_text, reply_markup_or_None) for the next step."""
    if step_index >= len(META_FIELDS):
        return None, None, None
    state, key, num, prompt, options = META_FIELDS[step_index]
    full_prompt = f"<b>Step {num}/15 — {prompt}</b>"
    markup = inline_kb(options, "meta") if options else None
    return state, full_prompt, markup


async def _handle_meta_text(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int) -> int:
    """Generic handler for text-based meta fields."""
    idx = _META_BY_STATE.get(current_state)
    if idx is None:
        return current_state
    _, key, *_ = META_FIELDS[idx]
    context.user_data["_meta"][key] = update.message.text.strip()

    # Move to next
    next_idx = idx + 1
    next_state, prompt, markup = _next_meta_prompt(next_idx)
    if next_state is None:
        return await _meta_done(update, context)
    await update.message.reply_text(prompt, parse_mode="HTML", reply_markup=markup)
    return next_state


async def _handle_meta_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int) -> int:
    """Generic handler for callback-based meta fields (property type, furnished)."""
    query = update.callback_query
    await query.answer()
    idx = _META_BY_STATE.get(current_state)
    if idx is None:
        return current_state
    _, key, *_ = META_FIELDS[idx]
    value = query.data.split(":", 1)[1]
    context.user_data["_meta"][key] = value

    next_idx = idx + 1
    next_state, prompt, markup = _next_meta_prompt(next_idx)
    if next_state is None:
        return await _meta_done_from_callback(query, context)
    await query.edit_message_text(prompt, parse_mode="HTML", reply_markup=markup)
    return next_state


async def _meta_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """All 15 meta fields collected. Move to zone setup."""
    await update.message.reply_text(
        "✅ <b>Meta data complete!</b>\n\n"
        "Now let's define the inspection zones.\n\n"
        "📍 <b>Enter the name of Zone 1</b>\n(e.g. Entrance, Master Bedroom, Kitchen):",
        parse_mode="HTML",
    )
    context.user_data["_zone_count"] = 0
    context.user_data["_zones_setup"] = []
    return SETUP_ZONE_NAME


async def _meta_done_from_callback(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    await query.edit_message_text(
        "✅ <b>Meta data complete!</b>\n\n"
        "Now let's define the inspection zones.\n\n"
        "📍 <b>Enter the name of Zone 1</b>\n(e.g. Entrance, Master Bedroom, Kitchen):",
        parse_mode="HTML",
    )
    context.user_data["_zone_count"] = 0
    context.user_data["_zones_setup"] = []
    return SETUP_ZONE_NAME


# Individual meta handlers — they all delegate to the generic ones
async def h_date(u, c):       return await _handle_meta_text(u, c, DATE)
async def h_project(u, c):    return await _handle_meta_text(u, c, PROJECT_NAME)
async def h_unit(u, c):       return await _handle_meta_text(u, c, UNIT_NUMBER)
async def h_type(u, c):       return await _handle_meta_callback(u, c, PROPERTY_TYPE)
async def h_client(u, c):     return await _handle_meta_text(u, c, CLIENT_NAME)
async def h_email(u, c):      return await _handle_meta_text(u, c, CLIENT_EMAIL)
async def h_reason(u, c):     return await _handle_meta_text(u, c, REASON)
async def h_inspector(u, c):  return await _handle_meta_text(u, c, INSPECTOR)
async def h_address(u, c):    return await _handle_meta_text(u, c, ADDRESS)
async def h_developer(u, c):  return await _handle_meta_text(u, c, DEVELOPER)
async def h_area(u, c):       return await _handle_meta_text(u, c, TOTAL_AREA)
async def h_floor(u, c):      return await _handle_meta_text(u, c, FLOOR_NUMBER)
async def h_rooms(u, c):      return await _handle_meta_text(u, c, NUM_ROOMS)
async def h_furnished(u, c):  return await _handle_meta_callback(u, c, FURNISHED)
async def h_year(u, c):       return await _handle_meta_text(u, c, YEAR_BUILT)


# ══════════════════════════════════════════════════════════════════════════════
#  ZONE SETUP (lead only)
# ══════════════════════════════════════════════════════════════════════════════

async def setup_zone_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive zone name, ask for type."""
    name = update.message.text.strip()
    context.user_data["_zone_count"] += 1
    context.user_data["_pending_zone_name"] = name

    await update.message.reply_text(
        f"Zone {context.user_data['_zone_count']}: <b>{name}</b>\n\nWhat type?",
        parse_mode="HTML",
        reply_markup=inline_kb(["Regular", "MEP"], "ztype"),
    )
    return SETUP_ZONE_TYPE


async def setup_zone_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive zone type, ask for next zone or done."""
    query = update.callback_query
    await query.answer()
    ztype = query.data.split(":", 1)[1].lower()
    name = context.user_data.pop("_pending_zone_name", "Zone")

    context.user_data["_zones_setup"].append({"name": name, "type": ztype})

    zones_so_far = context.user_data["_zones_setup"]
    zone_list = "\n".join(
        f"  {i+1}. {z['name']} {'⚡' if z['type'] == 'mep' else '📍'}"
        for i, z in enumerate(zones_so_far)
    )

    await query.edit_message_text(
        f"<b>Zones defined:</b>\n{zone_list}\n\n"
        "📍 Enter name of next zone, or press ✅ Done:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done — start inspection", callback_data="zones:done")],
        ]),
    )
    return SETUP_ZONES_DONE


async def setup_zones_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed another zone name instead of pressing Done."""
    return await setup_zone_name(update, context)


async def setup_zones_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """All zones defined. Create inspection in Supabase, show join code."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    meta = context.user_data["_meta"]
    zones_setup = context.user_data["_zones_setup"]

    # Create inspection
    inspection = create_inspection(user_id, meta)
    inspection_id = inspection["id"]
    code = inspection["code"]

    # Add zones
    for i, z in enumerate(zones_setup, 1):
        add_zone(inspection_id, i, z["name"], z["type"])

    # Add lead as member
    add_member(inspection_id, user_id, meta.get("inspector", "Lead"), "lead")

    context.user_data["_inspection_id"] = inspection_id

    # Update inspection status
    update_inspection(inspection_id, status="active")

    zone_list = "\n".join(
        f"  {i+1}. {z['name']} {'⚡' if z['type'] == 'mep' else '📍'}"
        for i, z in enumerate(zones_setup)
    )

    await query.edit_message_text(
        f"✅ <b>Inspection created!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"📋 {len(zones_setup)} zones defined:\n{zone_list}\n\n"
        f"🔑 <b>Join code: <code>{code}</code></b>\n"
        f"Share this code with other inspectors.\n\n"
        f"Press Start to begin inspecting:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start inspecting", callback_data="zones:start")],
        ]),
    )
    return PICK_ZONE


async def _begin_zone_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Transition to zone picker after 'Start inspecting' button."""
    query = update.callback_query
    await query.answer()
    inspection_id = context.user_data.get("_inspection_id")
    return await _show_zone_picker(query, context, inspection_id)


async def _show_zone_picker(message_or_query, context, inspection_id: str) -> int:
    """Show the zone selection keyboard."""
    user_id = str(message_or_query.from_user.id) if hasattr(message_or_query, "from_user") else str(context._user_id)
    zones = get_zones(inspection_id)
    context.user_data["_inspection_id"] = inspection_id

    kb = zone_picker_kb(zones, user_id)

    # Count progress
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_zones = len(zones)
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    text = (
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{total_zones} zones done | {total_defects} defects total"
    )

    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_query.reply_text(text, parse_mode="HTML", reply_markup=kb)

    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  JOIN INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

async def join_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User entered a join code."""
    code = update.message.text.strip().upper()

    inspection = get_inspection_by_code(code)
    if not inspection:
        await update.message.reply_text(
            "❌ Code not found. Check the code and try again:",
            parse_mode="HTML",
        )
        return JOIN_CODE

    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "Inspector"

    # Add as member
    add_member(inspection["id"], user_id, user_name, "inspector")
    context.user_data["_inspection_id"] = inspection["id"]

    meta = inspection.get("meta", {})
    await update.message.reply_text(
        f"✅ <b>Joined!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"Press Start to pick a zone:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start inspecting", callback_data="zones:start")],
        ]),
    )
    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  ZONE PICKING & INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

async def zone_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle zone picker callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data  # zone:pick:<id> / zone:done:<id> / zone:taken:<id> / zone:finish

    if data == "zone:finish":
        return await _try_finish(query, context)

    if data.startswith("zone:start"):
        # "Start inspecting" button
        inspection_id = context.user_data.get("_inspection_id")
        return await _show_zone_picker(query, context, inspection_id)

    parts = data.split(":", 2)
    action = parts[1]
    zone_id = parts[2]

    if action == "done":
        await query.answer("This zone is already completed.", show_alert=True)
        return PICK_ZONE

    if action == "taken":
        await query.answer("This zone is being inspected by another team member.", show_alert=True)
        return PICK_ZONE

    if action == "pick":
        user_id = str(update.effective_user.id)
        zone = get_zone_by_id(zone_id)

        # Assign zone to this user (or re-enter if already assigned)
        update_zone(zone_id, assigned_to=user_id, status="in_progress")
        context.user_data["_current_zone_id"] = zone_id

        # Show zone info
        is_mep = zone["type"] == "mep"
        context.user_data["_is_mep"] = is_mep

        if is_mep:
            checklist_text = _get_mep_checklist_text(zone["name"])
            existing = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"⚡ <b>MEP Zone {zone['zone_number']}: {zone['name']}</b>\n\n"
                f"{checklist_text}"
                f"{'📸 ' + str(existing) + ' items already recorded. ' if existing else ''}"
                "📸 Send photo of each item you're testing, then pick <b>compliant</b> or a defect severity.\n\n"
                "Send a photo, or /skip if no photo is needed:",
                parse_mode="HTML",
            )
        else:
            existing = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"📍 <b>Zone {zone['zone_number']}: {zone['name']}</b>\n\n"
                f"{'📸 ' + str(existing) + ' defects already recorded. ' if existing else ''}"
                "📸 Send a photo of the defect.\n"
                "Or /skip if no photo available:",
                parse_mode="HTML",
            )
        return DEFECT_PHOTO

    return PICK_ZONE


def _get_mep_checklist_text(zone_name: str) -> str:
    """Return formatted MEP checklist for this zone type."""
    key = zone_name.lower().strip()
    for k, items in MEP_CHECKLISTS.items():
        if k in key:
            lines = "\n".join(f"  ☐ {item}" for item in items)
            return f"📋 <b>Checklist:</b>\n{lines}\n\n"
    # If no match, show all categories
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  DEFECT FLOW: photo → AI → confirm → severity → description → after
# ══════════════════════════════════════════════════════════════════════════════

async def defect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive defect photo → go straight to manual severity selection (no AI classification)."""
    if not update.message.photo:
        await update.message.reply_text("📸 Please send a photo. Or /skip if no photo available.")
        return DEFECT_PHOTO

    # Get the largest photo
    photo = update.message.photo[-1]

    # Store photo file_id (we no longer need bytes in memory — AI vision was removed)
    context.user_data["_temp_photo_file_id"] = photo.file_id

    await update.message.reply_text(
        "📸 Photo received.\n\n<b>Select severity:</b>",
        parse_mode="HTML",
        reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"),
    )
    return DEFECT_SEVERITY


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """No photo available — go to manual severity anyway."""
    context.user_data["_temp_photo_file_id"] = None

    await update.message.reply_text(
        "Select severity:",
        reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"),
    )
    return DEFECT_SEVERITY


async def defect_severity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manual severity selection → ask for free-text description."""
    query = update.callback_query
    await query.answer()
    severity = query.data.split(":", 1)[1]
    context.user_data["_manual_severity"] = severity

    # For 'compliant' we still need a short description; offer default buttons to skip typing.
    if severity == "compliant":
        is_mep = context.user_data.get("_is_mep", False)
        default_buttons = [
            [InlineKeyboardButton("✅ Functional and compliant", callback_data="usedesc:Functional and compliant")],
        ]
        if not is_mep:
            # Regular zones get an additional, more general default
            default_buttons.append(
                [InlineKeyboardButton("✅ In good condition, no issues", callback_data="usedesc:In good condition, no issues")]
            )
        await query.edit_message_text(
            "Severity: <b>🟢 compliant</b>\n\n"
            "Type a short note, or tap a button to use a default:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(default_buttons),
        )
        return DEFECT_DESC

    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡"}.get(severity, "⚪")
    await query.edit_message_text(
        f"Severity: {sev_emoji} <b>{severity}</b>\n\n"
        "📝 Type the defect description:",
        parse_mode="HTML",
    )
    return DEFECT_DESC


async def defect_desc_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive typed description."""
    desc = update.message.text.strip()
    severity = context.user_data.get("_manual_severity", "medium")
    return await _save_defect_msg(update, context, severity, desc)


async def defect_desc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Use the canned default description (only shown for MEP compliant case in v4)."""
    query = update.callback_query
    await query.answer()
    desc = query.data.split(":", 1)[1]
    severity = context.user_data.get("_manual_severity", "medium")
    return await _save_defect(query, context, severity, desc)


async def _save_defect(query, context, severity: str, description: str) -> int:
    """Save defect to Supabase and show after-defect menu."""
    zone_id = context.user_data.get("_current_zone_id")
    photo_file_id = context.user_data.get("_temp_photo_file_id")

    defect = {
        "id": str(uuid4())[:8],
        "severity": severity,
        "description": clean_unicode(description),
        "photo_file_id": photo_file_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    append_defect_to_zone(zone_id, defect)

    # Clear temp
    context.user_data.pop("_temp_photo_file_id", None)
    context.user_data.pop("_manual_severity", None)

    zone = get_zone_by_id(zone_id)
    count = len(zone.get("defects") or [])
    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(severity, "⚪")

    buttons = [
        [InlineKeyboardButton("📸 Add another defect", callback_data="after:photo")],
        [InlineKeyboardButton("✏️ Edit a defect", callback_data="after:edit")],
        [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
        [InlineKeyboardButton("🗑 Delete last defect", callback_data="after:delete")],
        [InlineKeyboardButton("🏁 Finish inspection & generate PDF", callback_data="after:finish")],
    ]

    await query.edit_message_text(
        f"✅ Defect #{count} saved\n"
        f"{sev_emoji} {severity} — {description}\n\n"
        f"<b>Zone: {zone['name']}</b> ({count} defects)\n"
        "What's next?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AFTER_DEFECT


async def _save_defect_msg(update: Update, context, severity: str, description: str) -> int:
    """Save defect (from message context, not callback)."""
    zone_id = context.user_data.get("_current_zone_id")
    photo_file_id = context.user_data.get("_temp_photo_file_id")

    defect = {
        "id": str(uuid4())[:8],
        "severity": severity,
        "description": clean_unicode(description),
        "photo_file_id": photo_file_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    append_defect_to_zone(zone_id, defect)
    context.user_data.pop("_temp_photo_file_id", None)
    context.user_data.pop("_manual_severity", None)

    zone = get_zone_by_id(zone_id)
    count = len(zone.get("defects") or [])
    sev_emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(severity, "⚪")

    buttons = [
        [InlineKeyboardButton("📸 Add another defect", callback_data="after:photo")],
        [InlineKeyboardButton("✏️ Edit a defect", callback_data="after:edit")],
        [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
        [InlineKeyboardButton("🗑 Delete last defect", callback_data="after:delete")],
        [InlineKeyboardButton("🏁 Finish inspection & generate PDF", callback_data="after:finish")],
    ]

    await update.message.reply_text(
        f"✅ Defect #{count} saved\n"
        f"{sev_emoji} {severity} — {description}\n\n"
        f"<b>Zone: {zone['name']}</b> ({count} defects)\n"
        "What's next?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AFTER_DEFECT


# ══════════════════════════════════════════════════════════════════════════════
#  AFTER DEFECT ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def after_defect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle after-defect menu."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "photo":
        zone = get_zone_by_id(context.user_data["_current_zone_id"])
        await query.edit_message_text(
            f"📍 <b>{zone['name']}</b>\n\n📸 Send photo of the next defect:",
            parse_mode="HTML",
        )
        return DEFECT_PHOTO

    elif action == "switch":
        inspection_id = context.user_data["_inspection_id"]
        return await _show_zone_picker(query, context, inspection_id)

    elif action == "edit":
        return await _show_defect_list_for_edit(query, context)

    elif action == "delete":
        zone_id = context.user_data["_current_zone_id"]
        deleted = delete_last_defect_from_zone(zone_id)
        if deleted:
            zone = get_zone_by_id(zone_id)
            n = len(zone.get("defects") or [])
            await query.edit_message_text(
                f"🗑 Last defect deleted. Zone <b>{zone['name']}</b> now has {n} defects.\n\n"
                "📸 Send next photo, or:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
                    [InlineKeyboardButton("🏁 Finish inspection", callback_data="after:finish")],
                ]),
            )
            return DEFECT_PHOTO
        else:
            await query.answer("No defects to delete.", show_alert=True)
            return AFTER_DEFECT

    elif action == "finish":
        return await _try_finish(query, context)

    return AFTER_DEFECT


# ══════════════════════════════════════════════════════════════════════════════
#  EDIT DEFECT
# ══════════════════════════════════════════════════════════════════════════════

async def _show_defect_list_for_edit(query, context) -> int:
    """Show list of defects in current zone for editing."""
    zone_id = context.user_data.get("_current_zone_id")
    zone = get_zone_by_id(zone_id)
    defects = zone.get("defects") or []

    if not defects:
        await query.answer("No defects to edit.", show_alert=True)
        return AFTER_DEFECT

    buttons = []
    for i, d in enumerate(defects):
        sev = d.get("severity", "?")
        desc = d.get("description", "?")[:30]
        emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(sev, "⚪")
        buttons.append([InlineKeyboardButton(
            f"{emoji} #{i+1}: {desc}",
            callback_data=f"editpick:{i}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="editpick:back")])

    await query.edit_message_text(
        f"✏️ <b>Edit defect — {zone['name']}</b>\n\nPick a defect to edit:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_PICK_DEFECT


async def edit_pick_defect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked a defect to edit."""
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 1)[1]

    if data == "back":
        # Go back to after-defect menu by showing zone status
        zone = get_zone_by_id(context.user_data["_current_zone_id"])
        n = len(zone.get("defects") or [])
        buttons = [
            [InlineKeyboardButton("📸 Add another defect", callback_data="after:photo")],
            [InlineKeyboardButton("✏️ Edit a defect", callback_data="after:edit")],
            [InlineKeyboardButton("🔄 Switch zone", callback_data="after:switch")],
            [InlineKeyboardButton("🗑 Delete last defect", callback_data="after:delete")],
            [InlineKeyboardButton("🏁 Finish inspection & generate PDF", callback_data="after:finish")],
        ]
        await query.edit_message_text(
            f"📍 <b>Zone: {zone['name']}</b> ({n} defects)\nWhat's next?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return AFTER_DEFECT

    defect_idx = int(data)
    context.user_data["_edit_defect_idx"] = defect_idx

    zone = get_zone_by_id(context.user_data["_current_zone_id"])
    defect = (zone.get("defects") or [])[defect_idx]
    sev = defect.get("severity", "?")
    desc = defect.get("description", "?")
    emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(sev, "⚪")

    buttons = [
        [InlineKeyboardButton("🟢 Compliant", callback_data="editsev:compliant")],
        [InlineKeyboardButton("🔴 Critical", callback_data="editsev:critical")],
        [InlineKeyboardButton("🟠 Medium", callback_data="editsev:medium")],
        [InlineKeyboardButton("🟡 Minor", callback_data="editsev:minor")],
        [InlineKeyboardButton("📝 Edit description", callback_data="editsev:editdesc")],
        [InlineKeyboardButton("🗑 Delete this defect", callback_data="editsev:delete")],
        [InlineKeyboardButton("⬅️ Back", callback_data="editsev:back")],
    ]

    await query.edit_message_text(
        f"✏️ <b>Editing defect #{defect_idx + 1}</b>\n\n"
        f"Current: {emoji} {sev} — {desc}\n\n"
        "Change severity, edit description, or delete:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_DEFECT_SEV


async def edit_defect_sev_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle severity change or other edit actions."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    zone_id = context.user_data["_current_zone_id"]
    defect_idx = context.user_data.get("_edit_defect_idx", 0)

    if action == "back":
        return await _show_defect_list_for_edit(query, context)

    if action == "delete":
        zone = get_zone_by_id(zone_id)
        defects = zone.get("defects") or []
        if 0 <= defect_idx < len(defects):
            defects.pop(defect_idx)
            update_zone(zone_id, defects=defects)
        zone = get_zone_by_id(zone_id)
        n = len(zone.get("defects") or [])
        await query.edit_message_text(
            f"🗑 Defect deleted. Zone <b>{zone['name']}</b> now has {n} defects.",
            parse_mode="HTML",
        )
        if n > 0:
            return await _show_defect_list_for_edit(query, context)
        # No defects left — back to photo
        inspection_id = context.user_data["_inspection_id"]
        return await _show_zone_picker_query(query, context, inspection_id)

    if action == "editdesc":
        await query.edit_message_text("📝 Type the new description:")
        return EDIT_DEFECT_DESC

    # Severity change
    new_sev = action  # critical / medium / minor / compliant
    update_defect_in_zone(zone_id, defect_idx, severity=new_sev)

    zone = get_zone_by_id(zone_id)
    defect = (zone.get("defects") or [])[defect_idx]
    emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(new_sev, "⚪")

    await query.edit_message_text(
        f"✅ Severity updated to {emoji} <b>{new_sev}</b>\n\n"
        f"Defect #{defect_idx + 1}: {defect.get('description', '?')}",
        parse_mode="HTML",
    )
    return await _show_defect_list_for_edit(query, context)


async def edit_defect_desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new description for a defect."""
    new_desc = update.message.text.strip()
    zone_id = context.user_data["_current_zone_id"]
    defect_idx = context.user_data.get("_edit_defect_idx", 0)

    update_defect_in_zone(zone_id, defect_idx, description=clean_unicode(new_desc))

    zone = get_zone_by_id(zone_id)
    defect = (zone.get("defects") or [])[defect_idx]
    sev = defect.get("severity", "?")
    emoji = {"critical": "🔴", "medium": "🟠", "minor": "🟡"}.get(sev, "⚪")

    await update.message.reply_text(
        f"✅ Description updated\n\n"
        f"Defect #{defect_idx + 1}: {emoji} {sev} — {new_desc}",
        parse_mode="HTML",
    )

    # Show defect list again
    n = len(zone.get("defects") or [])
    buttons = []
    for i, d in enumerate(zone.get("defects") or []):
        s = d.get("severity", "?")
        desc = d.get("description", "?")[:30]
        em = {"critical": "🔴", "medium": "🟠", "minor": "🟡", "compliant": "🟢"}.get(s, "⚪")
        buttons.append([InlineKeyboardButton(f"{em} #{i+1}: {desc}", callback_data=f"editpick:{i}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="editpick:back")])

    await update.message.reply_text(
        f"✏️ <b>Edit defect — {zone['name']}</b> ({n} defects)\n\nPick another defect or go back:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_PICK_DEFECT


async def _show_zone_picker_msg(update: Update, context, inspection_id: str) -> int:
    """Show zone picker from a message context."""
    user_id = str(update.effective_user.id)
    zones = get_zones(inspection_id)
    kb = zone_picker_kb(zones, user_id)
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    await update.message.reply_text(
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{len(zones)} zones done | {total_defects} defects total",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return PICK_ZONE


async def _show_zone_picker_query(query, context, inspection_id: str) -> int:
    """Show zone picker from a callback query context."""
    user_id = str(query.from_user.id)
    zones = get_zones(inspection_id)
    kb = zone_picker_kb(zones, user_id)
    done_zones = sum(1 for z in zones if z["status"] == "done")
    total_defects = sum(len(z.get("defects") or []) for z in zones)

    await query.message.reply_text(
        f"📋 <b>Pick a zone to inspect</b>\n\n"
        f"Progress: {done_zones}/{len(zones)} zones done | {total_defects} defects total",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return PICK_ZONE


# ══════════════════════════════════════════════════════════════════════════════
#  FINISH / PDF GENERATION
# ══════════════════════════════════════════════════════════════════════════════

async def _try_finish(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finish inspection — auto-mark all zones as done, generate PDF."""
    inspection_id = context.user_data.get("_inspection_id")
    zones = get_zones(inspection_id)

    # Auto-mark all non-done zones as done
    for z in zones:
        if z["status"] != "done":
            update_zone(z["id"], status="done")

    # Generate PDF
    await query.edit_message_text("⏳ <b>Generating report...</b>\n\nDownloading photos and building PDF...", parse_mode="HTML")

    inspection = get_inspection_by_id(inspection_id)
    meta = inspection.get("meta", {})

    # Count severities
    sev_counts = {"critical": 0, "medium": 0, "minor": 0, "compliant": 0}
    total = 0
    for z in zones:
        for d in (z.get("defects") or []):
            sev = d.get("severity", "minor")
            sev_counts[sev] = sev_counts.get(sev, 0) + 1
            if sev != "compliant":
                total += 1

    # Generate AI observation texts
    await query.message.reply_text("⏳ Generating AI observations...")
    ai_texts = await generate_ai_texts(meta, zones)

    # Download photos
    await query.message.reply_text("⏳ Downloading photos...")
    photos_dir = os.path.join(REPORT_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    photo_count = 0

    for z in zones:
        for d in (z.get("defects") or []):
            fid = d.get("photo_file_id")
            if fid:
                try:
                    tf = await context.bot.get_file(fid)
                    path = os.path.join(photos_dir, f"{fid}.jpg")
                    await tf.download_to_drive(path)
                    d["photo_path"] = path
                    photo_count += 1
                except Exception as e:
                    logger.warning(f"Photo download failed for {fid}: {e}")
                    d["photo_path"] = ""

    await query.message.reply_text(f"📸 {photo_count} photos downloaded. Building PDF...")

    # Build PDF using generate_v5_newtempl.py
    try:
        pdf_path = await _build_pdf(meta, zones, sev_counts, total, ai_texts)
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        # Save data to Supabase anyway
        update_inspection(inspection_id, status="complete")

        err_text = str(e).replace("<", "&lt;").replace(">", "&gt;")
        await query.message.reply_text(
            f"❌ PDF generation failed:\n\n{err_text}\n\n"
            f"Your data is safe in Supabase.\n"
            f"Run locally: python3 generate_from_supabase.py {meta.get('unit', '?')}",
        )
        return ConversationHandler.END

    # Mark complete
    update_inspection(inspection_id, status="complete")

    # Send PDF to ALL members
    members = get_members(inspection_id)
    summary = (
        f"✅ <b>Inspection Report Complete!</b>\n\n"
        f"🏗 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
        f"📅 {meta.get('date', '?')}\n"
        f"📊 {sev_counts['critical']} critical | {sev_counts['medium']} medium | {sev_counts['minor']} minor\n"
        f"📸 {photo_count} photos | {len(zones)} zones"
    )

    sent_count = 0
    fail_count = 0
    for member in members:
        chat_id = int(member["user_id"])
        sent = False
        for attempt in range(3):
            try:
                with open(pdf_path, "rb") as pdf_file:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=pdf_file,
                        filename=os.path.basename(pdf_path),
                        caption=summary,
                        parse_mode="HTML",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
                sent_count += 1
                sent = True
                logger.info(f"PDF sent to {chat_id}")
                break
            except Exception as e:
                logger.warning(f"PDF send attempt {attempt+1} to {chat_id} failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        if not sent:
            fail_count += 1
            logger.error(f"PDF delivery to {chat_id} failed after 3 attempts")

    if fail_count > 0:
        await query.message.reply_text(
            f"{summary}\n\n"
            f"📄 PDF sent to {sent_count}/{len(members)} member(s).\n"
            f"❌ {fail_count} delivery failed — check logs.",
            parse_mode="HTML",
        )
    else:
        await query.message.reply_text(
            f"{summary}\n\n📄 PDF sent to {sent_count} member(s).",
            parse_mode="HTML",
        )
    return ConversationHandler.END


async def _build_pdf(meta: dict, zones: list, sev_counts: dict, total: int, ai_texts: dict) -> str:
    """Build PDF using generate_v5_newtempl.py template injection."""

    # Look up AI-generated per-zone observation text (falls back to hardcoded template if missing)
    zone_obs_map = (ai_texts or {}).get("zone_obs") or {}

    # Build areas list — separate regular and MEP zones
    areas_list = []
    mep_areas_list = []

    for z in zones:
        is_mep_zone = z.get("type") == "mep"
        zn_key = str(z["zone_number"])

        if is_mep_zone:
            # MEP zones: include ALL items (compliant + defects) as checks
            checks = []
            mep_defects_count = 0
            for d in (z.get("defects") or []):
                sev = d.get("severity", "compliant")
                desc = trunc(d.get("description", ""), 100)
                checks.append({
                    "sev": sev,
                    "desc": desc,
                    "photo": d.get("photo_path", ""),
                })
                if sev != "compliant":
                    mep_defects_count += 1

            # Prefer AI-generated obs; fall back to hardcoded template
            obs = zone_obs_map.get(zn_key) or (
                f"The {z['name']} system has {mep_defects_count} issues noted requiring attention."
                if mep_defects_count > 0
                else f"All {z['name']} systems tested and found compliant. No issues detected."
            )

            mep_areas_list.append({
                "num": zn_key,
                "name": z["name"],
                "defects": checks,
                "checks": checks,
                "obs": trunc(obs, 450),
            })
        else:
            # Regular zones: include ALL items (compliant and defects).
            # v4: compliant comments are now allowed in regular zones too — inspector may want
            # to flag a positive observation. They render as green COMPLIANT cards.
            defects = []
            real_defects_count = 0
            for d in (z.get("defects") or []):
                sev = d.get("severity", "minor")
                defects.append({
                    "sev": sev,
                    "desc": trunc(d.get("description", ""), 100),
                    "photo": d.get("photo_path", ""),
                })
                if sev != "compliant":
                    real_defects_count += 1

            # Prefer AI-generated obs; fall back to hardcoded template
            if zone_obs_map.get(zn_key):
                obs = zone_obs_map[zn_key]
            elif real_defects_count > 0:
                defect_items = [d["desc"] for d in defects if d["sev"] != "compliant"]
                items = ", ".join(defect_items[:5])
                obs = f"The {z['name']} area has {real_defects_count} comments noted. Comments include {items}. Mentioned comments should be rectified prior to handover."
            else:
                obs = f"Overall, the {z['name']} is in good condition. No comments were noted during inspection."

            areas_list.append({
                "num": zn_key,
                "name": z["name"],
                "defects": defects,
                "obs": trunc(obs, 450),
            })

    # Texts
    summary_obs = ai_texts.get("summary_obs") or (
        f"Overall, the unit is in acceptable condition. A total of {total} comments were identified. "
        f"Mentioned comments should be rectified prior to {meta.get('reason', 'handover')}."
    )
    general_cond = ai_texts.get("general_condition") or (
        f"The property at {meta.get('address', '')} is in acceptable condition. "
        f"The inspection identified {total} comments across {len(zones)} zones."
    )
    urgent = ai_texts.get("urgent") or "No critical items identified."

    # Clean texts
    summary_obs = trunc(summary_obs, 432)   # 12 lines × 36 chars in PDF render
    general_cond = trunc(general_cond, 600)
    urgent = trunc(urgent, 350)

    def to_py(obj):
        """Convert Python object to Python literal string, ASCII-safe."""
        s = json.dumps(obj, indent=4, ensure_ascii=True)
        s = s.replace(": null", ": None").replace("null,", "None,")
        s = s.replace(": true", ": True").replace(": false", ": False")
        return s

    clean_meta = {}
    for k, v in meta.items():
        clean_meta[k] = trunc(str(v), 120)

    data_block = (
        "\n# " + "=" * 79 + "\n"
        "# REPORT DATA\n"
        "# " + "=" * 79 + "\n"
        "DATA = " + to_py(clean_meta) + "\n\n"
        "TOTALS = " + to_py({"critical": sev_counts["critical"], "medium": sev_counts["medium"],
                             "minor": sev_counts["minor"], "total": total,
                             "compliant": sev_counts.get("compliant", 0)}) + "\n\n"
        "SUMMARY_OBS = " + json.dumps(summary_obs, ensure_ascii=True) + "\n\n"
        "AREAS = " + to_py(areas_list) + "\n\n"
        "MEP_AREAS = " + to_py(mep_areas_list) + "\n\n"
        "GENERAL_COND = " + json.dumps(general_cond, ensure_ascii=True) + "\n\n"
        "URGENT = " + json.dumps(urgent, ensure_ascii=True) + "\n"
    )

    # Read template
    gen_path = os.path.join(ASSETS_DIR, "generate_v5_newtempl.py")
    if not os.path.exists(gen_path):
        gen_path = os.path.join(REPORT_DIR, "generate_v5_newtempl.py")
    if not os.path.exists(gen_path):
        gen_path = "/app/generate_v5_newtempl.py"

    with open(gen_path, "r") as f:
        template = f.read()

    # Inject data section using lambda to avoid \u issues
    new_script = re.sub(
        r'# [═=]+\n# REPORT DATA.*?# [═=]+\n.*?(?=\n# [═=]+\n# BUILD PDF|\nclass\b|\ndef\b)',
        lambda m: data_block,
        template,
        count=1,
        flags=re.DOTALL,
    )

    # Write and execute
    unit_s = re.sub(r'[^\w\-]', '_', meta.get("unit", "unknown"))
    date_s = meta.get("date", "nodate").replace(".", "-")
    proj_s = re.sub(r'[^\w\-]', '_', meta.get("project", "Report"))[:20]
    out_pdf = os.path.join(REPORT_DIR, f"Report_{proj_s}_{unit_s}_{date_s}.pdf")
    tmp_py = os.path.join(REPORT_DIR, "_generate_tmp.py")

    # Set output path — template uses variable named OUT
    new_script = re.sub(r'^OUT\s*=\s*.*$', f'OUT = r"{out_pdf}"', new_script, count=1, flags=re.MULTILINE)

    with open(tmp_py, "w", encoding="utf-8") as f:
        f.write(new_script)

    # Run
    proc = await asyncio.create_subprocess_exec(
        "python3", tmp_py,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"Generator failed (exit {proc.returncode}): {err[:500]}")

    if not os.path.exists(out_pdf):
        raise RuntimeError(f"PDF not found at {out_pdf}")

    file_size = os.path.getsize(out_pdf)
    logger.info(f"PDF generated: {out_pdf} ({file_size} bytes)")
    if file_size < 100:
        raise RuntimeError(f"PDF file too small ({file_size} bytes) — likely empty/corrupt")

    return out_pdf


# ══════════════════════════════════════════════════════════════════════════════
#  CANCEL / BACK
# ══════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("🛑 Inspection cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def back_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Go back to zone picker from any inspection state."""
    inspection_id = context.user_data.get("_inspection_id")
    if inspection_id:
        return await _show_zone_picker_msg(update, context, inspection_id)
    await update.message.reply_text("Nothing to go back to. Send /start to begin.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION SETUP
# ══════════════════════════════════════════════════════════════════════════════

def build_app():
    import warnings
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(15)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_MENU: [CallbackQueryHandler(start_menu_handler, pattern=r"^start:")],

            # Meta fields
            DATE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, h_date)],
            PROJECT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_project)],
            UNIT_NUMBER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, h_unit)],
            PROPERTY_TYPE: [CallbackQueryHandler(h_type, pattern=r"^meta:")],
            CLIENT_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, h_client)],
            CLIENT_EMAIL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_email)],
            REASON:        [MessageHandler(filters.TEXT & ~filters.COMMAND, h_reason)],
            INSPECTOR:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_inspector)],
            ADDRESS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, h_address)],
            DEVELOPER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_developer)],
            TOTAL_AREA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, h_area)],
            FLOOR_NUMBER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, h_floor)],
            NUM_ROOMS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, h_rooms)],
            FURNISHED:     [CallbackQueryHandler(h_furnished, pattern=r"^meta:")],
            YEAR_BUILT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, h_year)],

            # Zone setup
            SETUP_ZONE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_zone_name)],
            SETUP_ZONE_TYPE: [CallbackQueryHandler(setup_zone_type, pattern=r"^ztype:")],
            SETUP_ZONES_DONE: [
                CallbackQueryHandler(setup_zones_done, pattern=r"^zones:done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setup_zones_add_more),
            ],

            # Join
            JOIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_code_handler)],

            # Zone picking
            PICK_ZONE: [
                CallbackQueryHandler(zone_pick_handler, pattern=r"^zone:"),
                CallbackQueryHandler(_begin_zone_pick, pattern=r"^zones:start$"),
            ],

            # Defect flow (v4: no per-defect AI — photo → severity → description)
            DEFECT_PHOTO: [
                MessageHandler(filters.PHOTO, defect_photo),
                CommandHandler("skip", skip_photo),
            ],
            DEFECT_SEVERITY: [CallbackQueryHandler(defect_severity, pattern=r"^sev:")],
            DEFECT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, defect_desc_text),
                CallbackQueryHandler(defect_desc_callback, pattern=r"^usedesc:"),
            ],
            AFTER_DEFECT: [
                CallbackQueryHandler(after_defect_handler, pattern=r"^after:"),
                MessageHandler(filters.PHOTO, defect_photo),  # Quick photo = add another
            ],

            # Edit defect
            EDIT_PICK_DEFECT: [
                CallbackQueryHandler(edit_pick_defect_handler, pattern=r"^editpick:"),
            ],
            EDIT_DEFECT_SEV: [
                CallbackQueryHandler(edit_defect_sev_handler, pattern=r"^editsev:"),
            ],
            EDIT_DEFECT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_defect_desc_handler),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("back", back_command),
            CommandHandler("start", start),
        ],
        per_message=False,
    )

    app.add_handler(conv)
    return app


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Snaggit Bot v3 (multi-inspector) running.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    import time
    logger.info("Snaggit Bot v3 started.")
    while True:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped.")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5s...")
            time.sleep(5)
