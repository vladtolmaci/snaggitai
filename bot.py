"""
Build: 2026-03-30 08:27
Snaggit Inspection Bot v2
- Collects full inspection data including inspector info
- MEP zones via same flow with compliant-only defects
- AI-generated obs/summary/urgent via Anthropic API
- Auto-runs PDF generator and sends PDF to Telegram

pip install python-telegram-bot==21.9 anthropic httpx
"""

import json, os, logging, subprocess, asyncio, re, base64
from datetime import datetime
import httpx
try:
    from supabase import create_client as _sb_create
    _SUPABASE = _sb_create(
        "https://hyyskrwaerayfaxlecdq.supabase.co",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5eXNrcndhZXJheWZheGxlY2RxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ1MDQ2MDEsImV4cCI6MjA5MDA4MDYwMX0.mawhRsDbmtUPsIHNFZyFDojsKo-Jz0A54OwGvlQV9jo"
    )
except Exception:
    _SUPABASE = None
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, PicklePersistence
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _startup_check():
    base = os.path.dirname(os.path.abspath(__file__))
    checks = [
        ('generate_v5_newtempl.py', os.path.join(base, 'generate_v5_newtempl.py')),
        ('fonts/', os.path.join(base, 'fonts')),
        ('tpl_v2/', os.path.join(base, 'tpl_v2')),
        ('Lexend-Light.ttf', os.path.join(base, 'fonts', 'Lexend-Light.ttf')),
        ('tpl_area.png', os.path.join(base, 'tpl_v2', 'tpl_area.png')),
    ]
    all_ok = True
    for name, path in checks:
        exists = os.path.exists(path)
        status = 'OK' if exists else 'MISSING'
        print(f"STARTUP CHECK: {name} -> {status} ({path})", flush=True)
        if not exists:
            all_ok = False
    print(f"STARTUP CHECK: all_ok={all_ok}", flush=True)
_startup_check()

# ── CONFIG ────────────────────────────────────────────────────────────────────
# ── Railway: set BOT_TOKEN, ANTHROPIC_KEY in Dashboard → Variables ───────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
# On Railway /app/data is ephemeral — photos go there between sessions
# PDF generation happens on local Mac via generate_from_supabase.py
REPORT_DIR     = os.environ.get("REPORT_DIR", "/app/data")
ASSETS_DIR     = os.path.dirname(os.path.abspath(__file__))  # /app — where fonts/ tpl_v2/ live
GENERATOR_PATH = os.path.join(ASSETS_DIR, "generate_v5_newtempl.py")
for _d in [REPORT_DIR, os.path.join(REPORT_DIR, "photos"), os.path.join(REPORT_DIR, "backups")]:
    os.makedirs(_d, exist_ok=True)

# ── STATES ────────────────────────────────────────────────────────────────────
(
    DATE, PROJECT_NAME, UNIT_NUMBER, PROPERTY_TYPE,
    CLIENT_NAME, CLIENT_EMAIL, REASON,
    INSPECTOR, ADDRESS, DEVELOPER,
    TOTAL_AREA, FLOOR_NUMBER, NUM_ROOMS, FURNISHED, YEAR_BUILT,
    ZONE_TYPE, ZONE_NAME,
    DEFECT_PHOTO, DEFECT_SEVERITY, DEFECT_DESC,
    AFTER_DEFECT,
    AI_CONFIRM,          # V2: AI photo analysis confirmation
    GO_BACK_ZONE,        # rollup zone picker for missed defects
    DELETE_DEFECT,       # pick defect to delete from zone
) = range(24)

PROPERTY_TYPES    = ["Apartment", "Villa", "Townhouse", "Penthouse", "Studio", "Duplex"]
FURNISHED_OPTIONS = ["Furnished", "Not Furnished", "Semi-Furnished"]
SEVERITY_OPTIONS  = ["minor", "medium", "critical", "compliant"]
SEV_EMOJI         = {"minor": "🟡", "medium": "🟠", "critical": "🔴", "compliant": "🟢"}
TOTAL_META_STEPS  = 15


# ── HELPERS ───────────────────────────────────────────────────────────────────
def init_report(context):
    context.user_data["report"] = {
        "meta": {}, "zones": [],
        "_current_zone": None, "_current_defects": [], "_zone_count": 0,
    }

def get_report(context):
    if "report" not in context.user_data:
        context.user_data["report"] = {"meta": {}, "zones": [], "_current_zone": None, "_current_defects": []}
    return context.user_data["report"]


def delete_session(user_id: int):
    """Remove active session from Supabase after report is complete."""
    if _SUPABASE:
        try:
            _SUPABASE.table("bot_sessions").delete().eq("user_id", str(user_id)).execute()
        except Exception as e:
            logger.warning(f"Session delete failed: {e}")


def backup_to_disk(context, user_id: int):
    """Save full report JSON to disk after every defect. Never loses data."""
    try:
        report = context.user_data.get("report", {})
        meta = report.get("meta", {})

        # Build a clean snapshot — include current open zone too
        def normalize_zone(zone: dict) -> dict:
            """Deep-copy zone and fix photo_path to always point to REPORT_DIR/photos."""
            z = dict(zone)
            fixed_defects = []
            for d in z.get("defects", []):
                d2 = dict(d)
                if d2.get("photo_file_id"):
                    # Always point to the master Mac's photos folder regardless of who shot it
                    d2["photo_path"] = os.path.join(REPORT_DIR, "photos", d2["photo_file_id"] + ".jpg")
                fixed_defects.append(d2)
            z["defects"] = fixed_defects
            return z

        all_zones = [normalize_zone(z) for z in report.get("zones", [])]

        # Include currently open zone too
        current_zone = report.get("_current_zone")
        if current_zone:
            current_snapshot = normalize_zone(current_zone)
            current_snapshot["defects"] = [dict(d) for d in report.get("_current_defects", [])]
            # Fix paths in current defects too
            for d in current_snapshot["defects"]:
                if d.get("photo_file_id"):
                    d["photo_path"] = os.path.join(REPORT_DIR, "photos", d["photo_file_id"] + ".jpg")
            current_snapshot["_status"] = "in_progress"
            all_zones.append(current_snapshot)

        snapshot = {
            "user_id": user_id,
            "saved_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "meta": meta,
            "zones": all_zones,
        }

        # One backup file per unit+date — overwrites on each save (always current)
        unit = meta.get("unit", "unknown").replace(" ", "_")
        date = meta.get("date", "nodate").replace(".", "-")
        backup_dir = os.path.join(REPORT_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        filepath = os.path.join(backup_dir, f"backup_{unit}_{date}.json")

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

        # Also save to Supabase
        if _SUPABASE:
            try:
                _SUPABASE.table("bot_sessions").upsert({
                    "user_id": str(user_id),
                    "data": snapshot,  # pass dict directly — supabase client handles jsonb serialization
                    "updated_at": datetime.utcnow().isoformat(),
                }, on_conflict="user_id").execute()
                _SUPABASE.table("report_backups").insert({
                    "user_id": str(user_id),
                    "unit": meta.get("unit", ""),
                    "project": meta.get("project", ""),
                    "inspector": meta.get("inspector", ""),
                    "date": meta.get("date", ""),
                    "data": snapshot,  # pass dict directly — supabase client handles jsonb serialization
                }).execute()
            except Exception as _se:
                logger.warning(f"Supabase backup failed: {_se}")

    except Exception as e:
        logger.warning(f"Backup failed: {e}")

def inline_kb(options, prefix):
    buttons = [InlineKeyboardButton(o.capitalize(), callback_data=f"{prefix}:{o}") for o in options]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)

def reply_kb(options):
    return ReplyKeyboardMarkup([[o] for o in options], resize_keyboard=True, one_time_keyboard=True)

def pb(step):
    filled = round(step / TOTAL_META_STEPS * 10)
    return "█" * filled + "░" * (10 - filled) + f"  {step}/{TOTAL_META_STEPS}"


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    init_report(context)
    await update.message.reply_text(
        "👋 <b>Snaggit Inspection Bot</b>\n\nI'll guide you through the full inspection report.\nType /cancel at any time to stop.",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text(
        f"<code>{pb(1)}</code>\n\n📅 <b>Date of Inspection</b>\nEnter date (e.g. 19.03.2026):", parse_mode="HTML")
    return DATE


# ── META STEPS ────────────────────────────────────────────────────────────────
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["date"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(2)}</code>\n\n🏗 <b>Project Name</b>", parse_mode="HTML")
    return PROJECT_NAME

async def get_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["project"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(3)}</code>\n\n🔢 <b>Unit Number</b>", parse_mode="HTML")
    return UNIT_NUMBER

async def get_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["unit"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(4)}</code>\n\n🏠 <b>Property Type</b>",
        parse_mode="HTML", reply_markup=reply_kb(PROPERTY_TYPES))
    return PROPERTY_TYPE

async def get_property_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["type"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(5)}</code>\n\n👤 <b>Client Name</b>",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    return CLIENT_NAME

async def get_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["client"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(6)}</code>\n\n📧 <b>Client Email</b>", parse_mode="HTML")
    return CLIENT_EMAIL

async def get_client_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["email"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(7)}</code>\n\n📋 <b>Reason for Inspection</b>", parse_mode="HTML")
    return REASON

async def get_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["reason"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(8)}</code>\n\n🧑‍🔧 <b>Inspector Name</b>", parse_mode="HTML")
    return INSPECTOR

async def get_inspector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["inspector"] = update.message.text.strip()
    await update.message.reply_text(
        f"<code>{pb(9)}</code>\n\n📍 <b>Property Address</b>\n(e.g. Sobha Hartland, MBR City, Dubai)", parse_mode="HTML")
    return ADDRESS

async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["address"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(10)}</code>\n\n🏢 <b>Developer</b>\n(e.g. Emaar, Sobha, Damac)", parse_mode="HTML")
    return DEVELOPER

async def get_developer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["developer"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(11)}</code>\n\n📐 <b>Total Area</b>\n(e.g. 1200 sq ft)", parse_mode="HTML")
    return TOTAL_AREA

async def get_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["area"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(12)}</code>\n\n🏢 <b>Floor Number</b>", parse_mode="HTML")
    return FLOOR_NUMBER

async def get_floor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["floor"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(13)}</code>\n\n🛏 <b>Number of Rooms</b>\n(e.g. 1 Bedroom)", parse_mode="HTML")
    return NUM_ROOMS

async def get_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["rooms"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(14)}</code>\n\n🛋 <b>Furnished?</b>",
        parse_mode="HTML", reply_markup=reply_kb(FURNISHED_OPTIONS))
    return FURNISHED

async def get_furnished(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["furnished"] = update.message.text.strip()
    await update.message.reply_text(f"<code>{pb(15)}</code>\n\n📅 <b>Year Built</b>",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    return YEAR_BUILT

async def get_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    get_report(context)["meta"]["year"] = update.message.text.strip()
    meta = get_report(context)["meta"]
    await update.message.reply_text(
        f"✅ <b>Property Info saved!</b>\n\n"
        f"📅 {meta['date']} | 🏗 {meta['project']} | 🔢 Unit {meta['unit']}\n"
        f"👤 {meta['client']} | 🧑‍🔧 {meta['inspector']}\n"
        f"📍 {meta['address']}\n\n"
        "──────────────────\n"
        "Now let's add <b>zones</b>.\n\nWhat type is Zone 1?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Regular Zone", callback_data="zonetype:regular")],
            [InlineKeyboardButton("⚡ MEP Zone (Electrical/HVAC/Plumbing)", callback_data="zonetype:mep")],
        ])
    )
    get_report(context)["_zone_count"] = 1
    return ZONE_TYPE


# ── ZONE TYPE ─────────────────────────────────────────────────────────────────
async def get_zone_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    zone_type = query.data.split(":")[1]
    report = get_report(context)
    report["_current_zone_type"] = zone_type
    zone_num = report["_zone_count"]
    await query.message.reply_text(f"📍 <b>Zone {zone_num}</b> — Enter zone name:", parse_mode="HTML")
    return ZONE_NAME


# ── ZONE & DEFECT FLOW ────────────────────────────────────────────────────────
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

def get_mep_checklist_text(zone_name: str) -> str:
    key = zone_name.lower().strip()
    for k, items in MEP_CHECKLISTS.items():
        if k in key:
            lines = "\n".join(f"  ☐ {item}" for item in items)
            return f"📋 <b>Checklist:</b>\n{lines}\n\n"
    return ""


async def get_zone_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    report = get_report(context)
    zone_num = report["_zone_count"]
    zone_type = report.get("_current_zone_type", "regular")
    zone_name = update.message.text.strip()
    report["_current_zone"] = {"zone_number": zone_num, "name": zone_name, "type": zone_type, "defects": []}
    report["_current_defects"] = []
    context.user_data["_temp_defect"] = {}

    if zone_type == "mep":
        checklist = get_mep_checklist_text(zone_name)
        await update.message.reply_text(
            f"⚡ <b>MEP Zone {zone_num}: {zone_name}</b>\n\n"
            f"{checklist}"
            "📸 Send photo of each item you're testing.\n"
            "AI will mark it <b>compliant</b> if functional, or flag a defect if something fails.\n\n"
            "Send first photo (or /skip_photo if no photo):",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"📍 <b>Zone {zone_num}: {zone_name}</b>\n\n"
            "📸 Send a photo of the first defect (or /skip_photo):",
            parse_mode="HTML"
        )
    return DEFECT_PHOTO

# ── AI DEFECT ANALYSIS ────────────────────────────────────────────────────────
DEFECT_ANALYSIS_PROMPT = """You are a professional property snagging inspector in Dubai.
Your job: look at this photo and identify the ONE most significant defect visible.

STEP 1 — IGNORE THESE COMPLETELY:
- Any sticker, coloured dot, numbered label, tape, or marker = inspector's own snagging sticker, not a defect
- Furniture, personal items, curtains, appliances (unless the appliance itself is damaged)

STEP 2 — IDENTIFY the specific defect. Be precise:
Look for: paint issues (run/drip/splash/peel/missing patch/roller mark/overspray) | silicone/sealant (gap/crack/missing/stained) | tile (crack/chip/grout gap/lippage/stain) | plaster (crack/undulation/hollow) | joinery (misaligned door/wardrobe/drawer won't close/gap) | skirting (loose/gap/not flush) | surface marking (scratch/scuff/gouge on door frame/wall/floor) | debris/dirt/paint splash | structural crack

STEP 3 — DESCRIBE IT in exactly 2-5 words using this format:
[What it is] [where on the surface, only if needed to be specific]

REAL EXAMPLES from Dubai snagging reports:
- Paint run on wall → "Paint run"
- Silicone missing at bath edge → "Silicone gap at bath"
- Tile cracked near drain → "Tile crack"
- Door frame has a scratch → "Surface marking on frame"
- Wardrobe door doesn't align → "Wardrobe door misaligned"
- Grout missing between floor tiles → "Grout gap"
- Paint didn't reach corner → "Paint incomplete at corner"
- Wall has hollow sound/bulge → "Plaster undulation"
- Skirting board coming off → "Skirting loose"
- Paint splashed onto tile → "Paint splash on tile"
- Crack in plaster → "Plaster crack"
- Silicone stained/discoloured → "Silicone stained"
- Chip in tile edge → "Tile chip"
- Gap under door → "Gap at door base"

SEVERITY:
- critical: structural crack through wall/floor, active water leak/damp stain, exposed wiring, broken glass, lock won't work
- medium: paint run/drip/large patch missing, silicone gap at wet area junction, tile crack/grout gap, warped/misaligned door or cabinet, plaster undulation (visible bulge), loose fixture
- minor: small scratch/scuff under 5cm, paint splash on tile, dust/debris, hairline surface crack, slight overspray

NEVER use words: damage, risk, poor, bad, broken, defect, issue, problem, compromised, concern
Use instead: surface marking, gap, crack, run, incomplete, loose, misaligned, stained

Return ONLY raw JSON:
{
  "severity": "minor" | "medium" | "critical",
  "description": "2-5 words exactly as shown in examples above",
  "confidence": "high" | "medium" | "low"
}"""

MEP_DEFECT_ANALYSIS_PROMPT = """You are a professional property snagging inspector in Dubai testing MEP systems.

STICKER RULE: Any sticker, coloured dot, tape, or numbered label = inspector marker. Ignore completely.

This is a functional check photo. The question is: does this item WORK correctly?

STEP 1 — Identify what is being shown (socket, light, AC unit, tap, drain, pipe, DB panel, etc.)
STEP 2 — Assess: is it functional and in acceptable condition?

Return ONLY raw JSON:
{
  "severity": "compliant" | "minor" | "medium" | "critical",
  "description": "2-5 words — what the item is and its status",
  "confidence": "high" | "medium" | "low"
}

EXAMPLES:
- Socket works fine → severity: "compliant", description: "Socket functional"
- AC powers on, cold air → severity: "compliant", description: "AC unit functional"  
- Tap dripping when closed → severity: "medium", description: "Tap dripping"
- Socket plate loose → severity: "medium", description: "Socket plate loose"
- No water from tap → severity: "critical", description: "Tap no flow"
- Light not working → severity: "critical", description: "Light not functional"
- Scratched socket cover → severity: "minor", description: "Socket cover scratched"

SEVERITY for MEP:
- compliant: works correctly, acceptable condition
- critical: completely non-functional (no power/water/airflow, active leak)
- medium: works but has a visible fault (dripping, loose, noisy, partial function)
- minor: works fine but cosmetic issue only (scratch, missing label, loose cover)"""


async def analyse_photo_with_claude(photo_path: str, is_mep: bool = False) -> dict:
    with open(photo_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")
    prompt = MEP_DEFECT_ANALYSIS_PROMPT if is_mep else DEFECT_ANALYSIS_PROMPT
    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 300,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data},
                                },
                                {"type": "text", "text": prompt}
                            ],
                        }]
                    }
                )
                data = response.json()
                if "error" in data:
                    raise RuntimeError(f"Anthropic API error: {data['error']}")
                if "content" not in data:
                    raise RuntimeError(f"Unexpected API response: {list(data.keys())}")
                text = data["content"][0]["text"].strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                return json.loads(text.strip())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_err = e
            wait = 3 * (attempt + 1)
            logger.warning(f"API attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 3 * (attempt + 1)
            logger.warning(f"API attempt {attempt+1} error: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
    raise last_err



async def get_defect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        context.user_data["_temp_defect"] = {"photo_file_id": None, "photo_path": None}
        await update.message.reply_text("🏷 <b>Severity</b> — select defect type:",
            parse_mode="HTML", reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"))
        return DEFECT_SEVERITY

    photo = update.message.photo[-1]
    photos_dir = f"{REPORT_DIR}/photos"
    os.makedirs(photos_dir, exist_ok=True)
    filename = f"{photos_dir}/{photo.file_id}.jpg"
    file = await context.bot.get_file(photo.file_id)
    await file.download_to_drive(filename)
    context.user_data["_temp_defect"] = {"photo_file_id": photo.file_id, "photo_path": filename}

    # Detect if current zone is MEP
    report = get_report(context)
    is_mep = report.get("_current_zone_type", "regular") == "mep"

    thinking_msg = await update.message.reply_text("🤖 Analysing photo...")
    try:
        ai_result = await analyse_photo_with_claude(filename, is_mep=is_mep)
        sev   = ai_result.get("severity", "minor")
        desc  = ai_result.get("description", "")
        dtype = ai_result.get("defect_type", "")
        conf  = ai_result.get("confidence", "medium")
        context.user_data["_ai_suggestion"] = ai_result

        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "🟡")

        # For compliant MEP — fast confirm, no override options needed
        if sev == "compliant":
            await thinking_msg.edit_text(
                f"🤖 <b>AI Analysis</b> {conf_icon} <i>{conf} confidence</i>\n\n"
                f"🟢 <b>COMPLIANT</b> — {desc}\n\n"
                "Confirm or override:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm compliant", callback_data="aiconf:confirm")],
                    [InlineKeyboardButton("⚠️ Mark as defect instead", callback_data="aiconf:override")],
                ])
            )
            return AI_CONFIRM

        caption = (
            f"🤖 <b>AI Analysis</b> {conf_icon} <i>{conf} confidence</i>\n\n"
            f"Severity:       {SEV_EMOJI[sev]} <b>{sev.upper()}</b>\n"
            f"Type:              <i>{dtype}</i>\n"
            f"Description:  <i>{desc}</i>\n\n"
            "Confirm or override:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="aiconf:confirm")],
            [InlineKeyboardButton("✏️ Change severity", callback_data="aiconf:edit_sev")],
            [InlineKeyboardButton("📝 Change description", callback_data="aiconf:edit_desc")],
            [InlineKeyboardButton("🔄 Override everything", callback_data="aiconf:override")],
        ])

        await thinking_msg.edit_text(caption, parse_mode="HTML", reply_markup=keyboard)

        return AI_CONFIRM

    except Exception as e:
        logger.warning(f"AI photo analysis failed: {e}")
        await thinking_msg.edit_text(
            "⚠️ AI analysis failed. Select severity manually:",
            reply_markup=inline_kb(SEVERITY_OPTIONS, "sev")
        )
        return DEFECT_SEVERITY


async def handle_ai_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    ai = context.user_data.get("_ai_suggestion", {})

    if action == "confirm":
        context.user_data["_temp_defect"]["severity"]    = ai.get("severity", "minor")
        context.user_data["_temp_defect"]["description"] = ai.get("description", "")
        return await save_defect_and_ask(query.message, context)

    elif action == "edit_sev":
        context.user_data["_temp_defect"]["description"] = ai.get("description", "")
        await query.message.reply_text(
            f"AI suggested: <i>{ai.get('description','')}</i>\n\n🏷 Pick the correct severity:",
            parse_mode="HTML", reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"))
        return DEFECT_SEVERITY

    elif action == "edit_desc":
        context.user_data["_temp_defect"]["severity"]    = ai.get("severity", "minor")
        await query.message.reply_text(
            f"{SEV_EMOJI.get(ai.get('severity','minor'), '')} Severity <b>{ai.get('severity','minor').upper()}</b> kept.\n\n"
            "📝 Type the correct description (or /skip_desc):",
            parse_mode="HTML")
        return DEFECT_DESC

    else:  # override
        await query.message.reply_text(
            "🏷 <b>Severity</b> — select defect type:",
            parse_mode="HTML", reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"))
        return DEFECT_SEVERITY

async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_temp_defect"] = {"photo_file_id": None, "photo_path": None}
    await update.message.reply_text("🏷 <b>Severity</b> — select defect type:",
        parse_mode="HTML", reply_markup=inline_kb(SEVERITY_OPTIONS, "sev"))
    return DEFECT_SEVERITY

async def get_defect_severity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    severity = query.data.split(":")[1]
    context.user_data["_temp_defect"]["severity"] = severity
    await query.message.reply_text(
        f"{SEV_EMOJI[severity]} <b>{severity.upper()}</b> selected.\n\n📝 Describe the defect (or /skip_desc):",
        parse_mode="HTML")
    return DEFECT_DESC

async def get_defect_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_temp_defect"]["description"] = update.message.text.strip()
    return await save_defect_and_ask(update, context)

async def skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_temp_defect"]["description"] = ""
    return await save_defect_and_ask(update, context)

async def save_defect_and_ask(update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Works with both Message and CallbackQuery.message (for AI confirm flow)."""
    report = get_report(context)
    defect = context.user_data["_temp_defect"].copy()

    # ── DEDUP: only block if EXACT same photo_file_id (bot restart duplicate) ──
    existing = report["_current_defects"]
    new_photo = defect.get("photo_file_id")
    if new_photo:
        for d in existing:
            if d.get("photo_file_id") == new_photo:
                logger.info(f"Duplicate photo blocked: {new_photo[:20]}")
                msg = update if hasattr(update, "reply_text") else update.message
                await msg.reply_text(
                    "\u26a0\ufe0f Photo already saved. Continuing...",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ Add another defect", callback_data="action:add_defect")],
                        [InlineKeyboardButton("📍 New zone",            callback_data="action:new_zone")],
                        [InlineKeyboardButton("🔍 Missed a defect",    callback_data="action:missed")],
                        [InlineKeyboardButton("✅ Finish report",       callback_data="action:finish")],
                    ])
                )
                return AFTER_DEFECT

    defect["defect_number"] = len(existing) + 1
    report["_current_defects"].append(defect)
    # Backup to disk after every defect saved
    try:
        user_id = update.from_user.id if hasattr(update, "from_user") and update.from_user else                   update.message.from_user.id if hasattr(update, "message") and update.message else 0
        backup_to_disk(context, user_id)
    except Exception as _be:
        logger.warning(f"Backup hook failed: {_be}")
    sev      = defect.get("severity", "")
    desc     = defect.get("description", "(no description)")
    dtype    = defect.get("defect_type", "")
    dtype_str = f" <i>[{dtype}]</i>" if dtype else ""
    msg = update if hasattr(update, "reply_text") else update.message

    # If we came from "missed defect" flow — save this zone and restore the previous one
    return_zone = context.user_data.pop("_return_zone", None)
    if return_zone and return_zone.get("zone"):
        # Save current (missed) zone back to zones list
        cur = report["_current_zone"]
        if cur:
            cur["defects"] = report["_current_defects"].copy()
            existing_idx = next((i for i, z in enumerate(report["zones"]) if z["zone_number"] == cur["zone_number"]), None)
            if existing_idx is not None:
                report["zones"][existing_idx] = cur
            else:
                report["zones"].append(cur)
        # Restore the zone we came from
        report["_current_zone"] = return_zone["zone"]
        report["_current_zone_type"] = return_zone["zone_type"]
        report["_current_defects"] = return_zone["defects"]
        context.user_data["_temp_defect"] = {}
        z = return_zone["zone"]
        await msg.reply_text(
            f"✅ Missed defect added.\n\n"
            f"↩️ <b>Back in Zone {z['zone_number']}: {z['name']}</b>\n\n"
            "📸 Continue — send next photo or choose:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add another defect", callback_data="action:add_defect")],
                [InlineKeyboardButton("📍 New zone",            callback_data="action:new_zone")],
                [InlineKeyboardButton("🔍 Missed a defect",    callback_data="action:missed")],
                [InlineKeyboardButton("✅ Finish report",       callback_data="action:finish")],
            ])
        )
        return AFTER_DEFECT

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another defect", callback_data="action:add_defect")],
        [InlineKeyboardButton("📍 New zone",            callback_data="action:new_zone")],
        [InlineKeyboardButton("🔍 Missed a defect",    callback_data="action:missed")],
        [InlineKeyboardButton("✅ Finish report",       callback_data="action:finish")],
    ])

    await msg.reply_text(
        f"✅ Defect #{defect['defect_number']} saved:\n"
        f"{SEV_EMOJI.get(sev, '')} <b>{sev.upper()}</b>{dtype_str}\n"
        f"{desc}\n\nWhat's next?",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    return AFTER_DEFECT

async def after_defect_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    report = get_report(context)

    if action == "add_defect":
        context.user_data["_temp_defect"] = {}
        await query.message.reply_text("📸 Send a photo of the next defect (or /skip_photo):", parse_mode="HTML")
        return DEFECT_PHOTO

    if action == "missed":
        # Show rollup of saved zones — remember current zone to return to
        zones = report.get("zones", [])
        if not zones:
            await query.message.reply_text("⚠️ No other zones saved yet.")
            return AFTER_DEFECT
        buttons = []
        for z in sorted(zones, key=lambda x: x["zone_number"]):
            counts = {}
            for d in z.get("defects", []):
                s = d.get("severity", "")
                counts[s] = counts.get(s, 0) + 1
            badge = " ".join(
                f"{SEV_EMOJI.get(s,'')}{counts[s]}"
                for s in ["critical","medium","minor","compliant"] if s in counts
            ) or "—"
            buttons.append([InlineKeyboardButton(
                f"{z['zone_number']}. {z['name']}  {badge}",
                callback_data=f"goback:{z['zone_number']}"
            )])
        await query.message.reply_text(
            "🔍 <b>Select zone with missed defect:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return GO_BACK_ZONE

    # For new_zone and finish — save current zone first
    if report.get("_current_zone"):
        zone = report["_current_zone"]
        zone["defects"] = report["_current_defects"].copy()
        existing_idx = next((i for i, z in enumerate(report["zones"]) if z["zone_number"] == zone["zone_number"]), None)
        if existing_idx is not None:
            report["zones"][existing_idx] = zone
        else:
            report["zones"].append(zone)
        report["_current_defects"] = []
        report["_current_zone"] = None

    if action == "new_zone":
        report["_zone_count"] = max((z["zone_number"] for z in report["zones"]), default=0) + 1
        zone_num = report["_zone_count"]
        await query.message.reply_text(
            f"📍 <b>Zone {zone_num}</b> — what type?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Regular Zone", callback_data="zonetype:regular")],
                [InlineKeyboardButton("⚡ MEP Zone", callback_data="zonetype:mep")],
            ])
        )
        return ZONE_TYPE

    if action == "finish":
        return await finish_report(update, context)


# ── AI TEXT GENERATION ────────────────────────────────────────────────────────
async def generate_texts_with_claude(report: dict) -> dict:
    zones_text = ""
    for z in report["zones"]:
        defects_list = "\n".join(
            f"  - [{d.get('severity','').upper()}] {d.get('description','')}"
            for d in z["defects"]
        )
        zones_text += f"\nZone {z['zone_number']}: {z['name']} ({z['type']})\n{defects_list}\n"

    prompt = f"""You are a professional property snagging inspector in Dubai writing an inspection report.

Property: {report['meta'].get('project','')} Unit {report['meta'].get('unit','')}
Client: {report['meta'].get('client','')}
Type: {report['meta'].get('type','')}
Reason: {report['meta'].get('reason','')}

Defects found:
{zones_text}

Generate the following as raw JSON only (no markdown, no code fences):
{{
  "zone_obs": {{
    "EXACT_ZONE_NAME": "zone observation text here"
  }},
  "summary_obs": "overall summary text",
  "general_condition": "general condition paragraph",
  "urgent": "urgent actions text"
}}

RULES for zone_obs:
- Write one entry per zone using the EXACT zone name as the key
- Each observation: 2-3 sentences
- Sentence 1: "Overall, the [zone name] is in [good/fair/acceptable] condition."
- Sentence 2: Name the specific comments found. E.g. "Comments noted include paint runs to the feature wall, a silicone gap at the bathtub junction, and minor tile grout gaps on the floor."
- Sentence 3: "Mentioned comments should be rectified prior to handover." (or move-in / re-letting depending on reason)
- If zone has NO defects: "Overall, the [zone name] is in good condition. No comments were noted during inspection."
- NEVER write generic text like "the zone was inspected" or "the area was reviewed"
- Use the word "comments" not "defects" or "issues"

RULES for summary_obs:
- 2-3 sentences summarising the whole property
- Mention total number of comments and overall condition

RULES for general_condition:
- 3-4 sentences, professional tone
- Mention the property type, overall standard, and key areas needing attention

RULES for urgent:
- Only list CRITICAL severity items
- Format: "1. [Zone] — [specific action needed]."
- If no critical items: "No critical items identified."

Zone names in zone_obs MUST exactly match the input zone names. Return raw JSON only."""

    last_err = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )
                data = response.json()
                if "error" in data:
                    raise RuntimeError(f"Anthropic API error: {data['error']}")
                if "content" not in data:
                    raise RuntimeError(f"Unexpected API response: {list(data.keys())}")
                text = data["content"][0]["text"].strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                return json.loads(text.strip())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_err = e
            wait = 5 * (attempt + 1)
            logger.warning(f"Text gen attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            logger.warning(f"Text gen attempt {attempt+1} error: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
    raise last_err


# ── PDF GENERATION ────────────────────────────────────────────────────────────
def build_generator_script(report: dict, ai_texts: dict, output_pdf: str) -> str:
    meta = report["meta"]

    regular_zones = [z for z in report["zones"] if z.get("type") != "mep"]
    mep_zones     = [z for z in report["zones"] if z.get("type") == "mep"]

    sev_counts = {"critical": 0, "medium": 0, "minor": 0, "compliant": 0}
    for z in report["zones"]:
        for d in z["defects"]:
            s = d.get("severity", "")
            if s in sev_counts:
                sev_counts[s] += 1
    # compliant does NOT count toward total defects
    total = sev_counts["critical"] + sev_counts["medium"] + sev_counts["minor"]

    def trunc(v, n=80): return str(v)[:n] if v else ""
    data_dict = {
        "date": trunc(meta.get("date",""), 20),
        "unit": trunc(meta.get("unit",""), 30),
        "project": trunc(meta.get("project",""), 60),
        "type": trunc(meta.get("type",""), 30),
        "inspector": trunc(meta.get("inspector",""), 50),
        "address": trunc(meta.get("address",""), 80),
        "client": trunc(meta.get("client",""), 80),
        "reason": trunc(meta.get("reason",""), 30),
        "email": trunc(meta.get("email",""), 80),
        "area": trunc(meta.get("area",""), 20),
        "furnished": trunc(meta.get("furnished",""), 20),
        "floor": trunc(meta.get("floor",""), 10),
        "year": trunc(meta.get("year",""), 10),
        "rooms": trunc(meta.get("rooms",""), 30),
        "developer": trunc(meta.get("developer",""), 60),
    }

    with open(GENERATOR_PATH, "r") as f:
        original = f.read()

    # Clean AI texts FIRST — remove newlines that break Python string literals
    def clean(t):
        if not t: return ""
        s = str(t).replace("\n", " ").replace("\r", " ")
        # encode to ascii to eliminate any unicode that would break generated python script
        return s.encode("ascii", "backslashreplace").decode("ascii").replace('"', '\\\"')
    ai_texts_clean = {}
    for k, v in ai_texts.items():
        if isinstance(v, str):
            ai_texts_clean[k] = clean(v)
        elif isinstance(v, dict):
            ai_texts_clean[k] = {kk: clean(vv) for kk, vv in v.items()}
        else:
            ai_texts_clean[k] = v

    areas_list = []
    for z in report["zones"]:
        zone_obs_dict = ai_texts_clean.get("zone_obs", {})
        # Try exact match first, then case-insensitive
        obs_text = zone_obs_dict.get(z["name"])
        if not obs_text:
            zname_lower = z["name"].strip().lower()
            obs_text = next((v for k, v in zone_obs_dict.items() if k.strip().lower() == zname_lower), None)
        if not obs_text:
            obs_text = f"Overall, the {z['name']} is in acceptable condition. No significant comments were noted during inspection."
        defects = [{"sev": d.get("severity","minor"), "desc": d.get("description","").replace("\n"," ").replace("\r"," ").encode("ascii","replace").decode("ascii").strip(), "photo": d.get("photo_path")} for d in z["defects"] if d.get("severity") != "compliant" or z.get("type") == "mep"]
        areas_list.append({"num": str(z["zone_number"]), "name": z["name"], "defects": defects, "obs": obs_text})

    ai_texts = ai_texts_clean
    mep_list = []  # MEP now handled via AREAS

    def to_py(obj):
        return json.dumps(obj, indent=4, ensure_ascii=True).replace(": null", ": None").replace("null,", "None,")

    n = "\n"
    data_section = (
        "# " + "="*79 + n +
        "# REPORT DATA - AUTO GENERATED BY SNAGGIT BOT" + n +
        "# " + "="*79 + n +
        "DATA = " + to_py(data_dict) + n + n +
        "TOTALS = " + to_py({"critical": sev_counts["critical"], "medium": sev_counts["medium"], "minor": sev_counts["minor"], "total": total, "compliant": sev_counts["compliant"]}) + n + n +
        "SUMMARY_OBS = " + json.dumps(ai_texts.get("summary_obs",""), ensure_ascii=True) + n + n +
        "AREAS = " + to_py(areas_list) + n + n +
        "MEP_AREAS = []" + n + n +
        "GENERAL_COND = " + json.dumps(ai_texts.get("general_condition",""), ensure_ascii=True) + n + n +
        "URGENT = " + json.dumps(ai_texts.get("urgent","").replace(chr(10)," ").replace(chr(13)," "), ensure_ascii=True) + n
    )

    # Try multiple patterns to handle different separator chars in generate script
    patterns = [
        r'# [=\u2550]+[\r\n]+# REPORT DATA[\s\S]*?(?=# [=\u2550]+[\r\n]+# BUILD PDF)',
        r'# REPORT DATA[\s\S]*?(?=# BUILD PDF)',
        r'DATA = \{[\s\S]*?URGENT = [^\n]+\n',
    ]
    new_script = original
    for pat in patterns:
        result = re.sub(pat, data_section + "\n", original, flags=re.DOTALL)
        if result != original:
            new_script = result
            logger.info(f"REPORT DATA replaced using pattern: {pat[:40]}")
            break
    else:
        logger.error("CRITICAL: Could not replace REPORT DATA section in generate script")

    # Update output path and resource paths
    new_script = re.sub(r'OUT\s*=\s*"[^"]*"', f'OUT   = {json.dumps(output_pdf)}', new_script)
    new_script = re.sub(r'TPL\s*=\s*"[^"]*"', f'TPL   = {json.dumps(ASSETS_DIR + "/tpl_v2")}', new_script)
    new_script = re.sub(r'FONTS\s*=\s*"[^"]*"', f'FONTS = {json.dumps(ASSETS_DIR + "/fonts")}', new_script)

    return new_script


# ── FINISH REPORT ─────────────────────────────────────────────────────────────
async def finish_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    report = get_report(context)
    msg = update.callback_query.message if update.callback_query else update.message

    sev_counts = {"minor": 0, "medium": 0, "critical": 0, "compliant": 0}
    for zone in report["zones"]:
        for d in zone["defects"]:
            s = d.get("severity","")
            if s in sev_counts:
                sev_counts[s] += 1

    await msg.reply_text(
        f"🎉 <b>Data collected!</b>\n\n"
        f"📍 {len(report['zones'])} zones | 🔴 {sev_counts['critical']} critical | "
        f"🟠 {sev_counts['medium']} medium | 🟡 {sev_counts['minor']} minor\n\n"
        "⏳ Generating AI observations...", parse_mode="HTML"
    )

    try:
        ai_texts = await generate_texts_with_claude(report)
        await msg.reply_text("✅ AI texts generated. Building PDF...")
    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        await msg.reply_text(f"⚠️ AI generation failed: {e}\nUsing placeholder texts.")
        ai_texts = {"zone_obs": {}, "summary_obs": "Inspection completed.", "general_condition": "The unit was inspected.", "urgent": "No critical items identified."}

    meta = report["meta"]
    unit_safe = meta.get("unit","unknown").replace(" ","_").replace("/","_").replace("\\","_").replace(":","_")
    date_safe = meta.get("date","unknown").replace(".","-")
    output_pdf = os.path.join(REPORT_DIR, f"Report_{unit_safe}_{date_safe}.pdf")
    tmp_script = os.path.join(REPORT_DIR, "_bot_generate_tmp.py")

    # ── Download photos from Telegram by file_id (works on Railway) ───────────
    await msg.reply_text("✅ AI texts ready. Downloading photos...")
    photos_dir = os.path.join(REPORT_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    downloaded = 0
    skipped = 0
    failed = 0
    all_fids = [(z["name"], d.get("photo_file_id")) for z in report.get("zones",[]) for d in z.get("defects",[]) if d.get("photo_file_id")]
    logger.info(f"Photos to download: {len(all_fids)}")
    for zone in report.get("zones", []):
        for d in zone.get("defects", []):
            fid = d.get("photo_file_id")
            if fid:
                local_path = os.path.join(photos_dir, fid + ".jpg")
                d["photo_path"] = local_path
                if os.path.exists(local_path):
                    skipped += 1
                else:
                    try:
                        tg_file = await context.bot.get_file(fid)
                        await tg_file.download_to_drive(local_path)
                        downloaded += 1
                    except Exception as _pe:
                        logger.warning(f"Photo download failed {fid[:30]}: {_pe}")
                        d["photo_path"] = None
                        failed += 1
    total_available = downloaded + skipped
    logger.info(f"Photos: downloaded={downloaded} skipped(cached)={skipped} failed={failed} total={total_available}")
    downloaded = total_available  # count cached photos too

    await msg.reply_text(f"📸 {downloaded} photos downloaded. Building PDF...")

    # ── Save to Supabase ───────────────────────────────────────────────────────
    try:
        if _SUPABASE:
            snap = {
                "user_id": str(update.effective_user.id),
                "saved_at": datetime.utcnow().isoformat(),
                "meta": meta, "zones": report.get("zones",[]),
                "ai_texts": ai_texts, "status": "complete",
            }
            _SUPABASE.table("report_backups").insert({
                "user_id": str(update.effective_user.id),
                "unit": meta.get("unit",""), "project": meta.get("project",""),
                "inspector": meta.get("inspector",""), "date": meta.get("date",""),
                "data": snap,  # pass dict directly — supabase client handles jsonb serialization
            }).execute()
    except Exception as _se:
        logger.warning(f"Supabase save failed: {_se}")

    # ── Generate PDF ───────────────────────────────────────────────────────────
    try:
        new_script = build_generator_script(report, ai_texts, output_pdf)
        with open(tmp_script, "w", encoding="utf-8") as f:
            f.write(new_script)

        proc = await asyncio.create_subprocess_exec(
            "python3", tmp_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("PDF generation timed out after 180 seconds")

        if proc.returncode != 0:
            raise RuntimeError(stderr.decode()[-600:])

        await msg.reply_text("✅ PDF ready! Sending...")
        with open(output_pdf, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=os.path.basename(output_pdf),
                caption=f"📄 Inspection Report\n{meta.get('project','')} — Unit {meta.get('unit','')}\n{meta.get('date','')}"
            )
        try:
            if _SUPABASE:
                _SUPABASE.table("bot_sessions").delete().eq("user_id", str(update.effective_user.id)).execute()
        except Exception as _de:
            logger.warning(f"Session delete failed: {_de}")
        await msg.reply_text("✅ Done! Type /start for a new report.")

    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        error_text = str(e)[:400].replace("<","").replace(">","")
        await msg.reply_text(
            f"❌ PDF generation failed:\n\n{error_text}\n\n"
            f"Your data is safe in Supabase. Run locally:\n"
            f"<code>python3 generate_from_supabase.py {meta.get('unit','')}</code>",
            parse_mode="HTML"
        )

    return ConversationHandler.END


# ── /missed — ROLLUP ZONE PICKER ─────────────────────────────────────────────
async def cmd_missed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show compact rollup of all zones — inspector taps one to add a missed defect."""
    report = get_report(context)

    # Save current open zone before showing rollup
    if report.get("_current_zone"):
        zone = report["_current_zone"]
        zone["defects"] = report["_current_defects"].copy()
        existing_nums = [z["zone_number"] for z in report["zones"]]
        if zone["zone_number"] in existing_nums:
            report["zones"][existing_nums.index(zone["zone_number"])] = zone
        else:
            report["zones"].append(zone)
        report["_current_defects"] = []
        report["_current_zone"] = None

    zones = report.get("zones", [])
    if not zones:
        await update.message.reply_text("⚠️ No zones saved yet.")
        return DEFECT_PHOTO

    buttons = []
    for z in sorted(zones, key=lambda x: x["zone_number"]):
        counts = {}
        for d in z.get("defects", []):
            s = d.get("severity", "")
            counts[s] = counts.get(s, 0) + 1
        badge = " ".join(
            f"{SEV_EMOJI.get(s, '')}×{c}"
            for s in ["critical", "medium", "minor", "compliant"]
            if s in counts
            for c in [counts[s]]
        ) or "—"
        label = f"Zone {z['zone_number']}: {z['name']}  {badge}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"goback:{z['zone_number']}")])

    await update.message.reply_text(
        "📋 <b>Select zone to add missed defect:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return GO_BACK_ZONE


async def handle_go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open selected zone, add missed defect, then return to the zone we came from."""
    query = update.callback_query
    await query.answer()
    zone_num = int(query.data.split(":")[1])
    report = get_report(context)

    target = next((z for z in report["zones"] if z["zone_number"] == zone_num), None)
    if not target:
        await query.message.reply_text("Zone not found.")
        return AFTER_DEFECT

    # Stash current open zone to return to it after adding missed defect
    context.user_data["_return_zone"] = {
        "zone": report.get("_current_zone"),
        "defects": list(report.get("_current_defects", [])),
        "zone_type": report.get("_current_zone_type", "regular"),
    }

    # Open the target zone for editing
    report["zones"] = [z for z in report["zones"] if z["zone_number"] != zone_num]
    report["_current_zone"] = dict(target)
    report["_current_zone_type"] = target.get("type", "regular")
    report["_current_defects"] = list(target.get("defects", []))
    context.user_data["_temp_defect"] = {}

    n = len(report["_current_defects"])
    await query.message.reply_text(
        f"🔍 <b>Zone {zone_num}: {target['name']}</b>  ({n} defect{'s' if n!=1 else ''} saved)\n\n"
        "📸 Send photo of missed defect (or /skip_photo):",
        parse_mode="HTML"
    )
    return DEFECT_PHOTO



# ── /backup — send current JSON to inspector ─────────────────────────────────
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send current session as JSON file to Telegram chat."""
    user_id = update.effective_user.id
    report = context.user_data.get("report", {})
    meta = report.get("meta", {})

    if not meta:
        await update.message.reply_text("⚠️ No active report to back up.")
        return

    def _norm_zone(zone):
        z = dict(zone)
        z["defects"] = [
            dict(d, photo_path=os.path.join(REPORT_DIR, "photos", d["photo_file_id"] + ".jpg"))
            if d.get("photo_file_id") else dict(d)
            for d in z.get("defects", [])
        ]
        return z

    all_zones = [_norm_zone(z) for z in report.get("zones", [])]
    current_zone = report.get("_current_zone")
    if current_zone:
        cur = _norm_zone(current_zone)
        cur["defects"] = [
            dict(d, photo_path=os.path.join(REPORT_DIR, "photos", d["photo_file_id"] + ".jpg"))
            if d.get("photo_file_id") else dict(d)
            for d in report.get("_current_defects", [])
        ]
        cur["_status"] = "in_progress"
        all_zones.append(cur)

    snapshot = {
        "saved_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "meta": meta,
        "zones": all_zones,
    }

    unit = meta.get("unit", "unknown").replace(" ", "_")
    date = meta.get("date", "nodate").replace(".", "-")
    filepath = os.path.join(REPORT_DIR, f"backup_{unit}_{date}.json")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

    zones_done = len(report.get("zones", []))
    defects_done = sum(len(z.get("defects", [])) for z in report.get("zones", []))
    current_defects = len(report.get("_current_defects", []))

    with open(filepath, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=os.path.basename(filepath),
            caption=(
                f"💾 <b>Backup saved</b>\n"
                f"📍 {meta.get('project', '?')} — Unit {meta.get('unit', '?')}\n"
                f"✅ {zones_done} zones, {defects_done} defects\n"
                f"🔄 Current zone: {current_defects} in progress"
            ),
            parse_mode="HTML"
        )
    # Also save to disk
    backup_to_disk(context, user_id)


# ── /delete — remove a defect from current zone ──────────────────────────────
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show list of defects in current zone with delete buttons."""
    report = get_report(context)
    defects = report.get("_current_defects", [])

    if not defects:
        await update.message.reply_text("No defects in current zone to delete.")
        return DEFECT_PHOTO

    buttons = []
    for d in defects:
        sev = d.get("severity", "")
        desc = d.get("description", "(no desc)")
        num = d.get("defect_number", "?")
        buttons.append([InlineKeyboardButton(
            f"{SEV_EMOJI.get(sev,'')} #{num} {desc}  🗑",
            callback_data=f"del:{num}"
        )])
    buttons.append([InlineKeyboardButton("↩️ Cancel", callback_data="del:cancel")])

    await update.message.reply_text(
        "🗑 <b>Select defect to delete:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return DELETE_DEFECT


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle defect deletion."""
    query = update.callback_query
    await query.answer()
    val = query.data.split(":")[1]

    if val == "cancel":
        await query.message.reply_text("Cancelled. Continue inspection:")
        return DEFECT_PHOTO

    num = int(val)
    report = get_report(context)
    defects = report.get("_current_defects", [])
    target = next((d for d in defects if d.get("defect_number") == num), None)

    if not target:
        await query.message.reply_text("Defect not found.")
        return DEFECT_PHOTO

    report["_current_defects"] = [d for d in defects if d.get("defect_number") != num]
    # Renumber remaining
    for i, d in enumerate(report["_current_defects"], 1):
        d["defect_number"] = i

    backup_to_disk(context, update.effective_user.id)

    sev_del = target.get("severity", "")
    desc_del = target.get("description", "")
    rem = len(report["_current_defects"])
    await query.message.reply_text(
        f"\U0001f5d1 Deleted: {SEV_EMOJI.get(sev_del, '')} {desc_del}\n\n"
        f"Remaining: {rem} defects in this zone.\n\nContinue:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add another defect", callback_data="action:add_defect")],
            [InlineKeyboardButton("📍 New zone",            callback_data="action:new_zone")],
            [InlineKeyboardButton("🔍 Missed a defect",    callback_data="action:missed")],
            [InlineKeyboardButton("✅ Finish report",       callback_data="action:finish")],
        ])
    )
    return AFTER_DEFECT


# ── CANCEL ────────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Report cancelled. Type /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── MAIN ──────────────────────────────────────────────────────────────────────
def build_app():
    persistence = PicklePersistence(filepath=f"{REPORT_DIR}/bot_persistence.pkl")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )
    import warnings
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning)
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        per_message=False,
        states={
            DATE:            [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            PROJECT_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_project)],
            UNIT_NUMBER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_unit)],
            PROPERTY_TYPE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_property_type)],
            CLIENT_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_name)],
            CLIENT_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_email)],
            REASON:          [MessageHandler(filters.TEXT & ~filters.COMMAND, get_reason)],
            INSPECTOR:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_inspector)],
            ADDRESS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            DEVELOPER:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_developer)],
            TOTAL_AREA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_area)],
            FLOOR_NUMBER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_floor)],
            NUM_ROOMS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_rooms)],
            FURNISHED:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_furnished)],
            YEAR_BUILT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_year)],
            ZONE_TYPE:       [CallbackQueryHandler(get_zone_type, pattern="^zonetype:")],
            ZONE_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_zone_name)],
            DEFECT_PHOTO: [
                MessageHandler(filters.PHOTO, get_defect_photo),
                CommandHandler("skip_photo", skip_photo),
                CommandHandler("missed", cmd_missed),
                CommandHandler("backup", cmd_backup),
                CommandHandler("delete", cmd_delete),
            ],
            AI_CONFIRM:      [CallbackQueryHandler(handle_ai_confirm, pattern="^aiconf:")],
            DEFECT_SEVERITY: [CallbackQueryHandler(get_defect_severity, pattern="^sev:")],
            DEFECT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_defect_desc),
                CommandHandler("skip_desc", skip_desc),
            ],
            AFTER_DEFECT:    [CallbackQueryHandler(after_defect_action, pattern="^action:"),
                               CommandHandler("missed", cmd_missed),
                               CommandHandler("backup", cmd_backup),
                               CommandHandler("delete", cmd_delete)],
            GO_BACK_ZONE:    [CallbackQueryHandler(handle_go_back, pattern="^goback:")],
            DELETE_DEFECT:   [CallbackQueryHandler(handle_delete, pattern="^del:")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("missed", cmd_missed), CommandHandler("backup", cmd_backup), CommandHandler("delete", cmd_delete)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    async def error_handler(update, context):
        err = context.error
        logger.error(f"Update {update} caused error: {err}")
        # NetworkError — bот сам восстановится, не нужно ничего делать
        from telegram.error import NetworkError, TimedOut
        if isinstance(err, (NetworkError, TimedOut)):
            return
        # Для всех остальных ошибок — попробуй уведомить пользователя
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Your data is safe — please try again."
                )
        except Exception:
            pass

    app.add_error_handler(error_handler)
    return app


async def main():
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Snaggit Bot v4 running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    import time
    logger.info("Snaggit Bot v4 started.")
    while True:
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped.")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5s...")
            time.sleep(5)
