"""
Snaggit v5 — Pixel-perfect PDF generator
Cedar Building 2, Unit 308

All coordinates measured directly from the reference PDF exported by the client.
Template PNGs used as backgrounds — no white-out needed.
"""
import os, textwrap
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image

# ── PATHS ─────────────────────────────────────────────────────────────────────
TPL   = "/home/claude/snaggit/tpl"
FONTS = "/home/claude/snaggit/fonts"
SRC   = "/home/claude/snaggit/Cedar_308_extracted"
OUT   = "/mnt/user-data/outputs/Cedar_Building2_Unit308_v5.pdf"

PW, PH = 595, 842   # A4 pt

# ── COLOURS ───────────────────────────────────────────────────────────────────
GREEN      = HexColor("#007E50")   # rgb(0,126,80) — exact from reference
DARK_GREEN = HexColor("#005C3A")
RED        = HexColor("#E8252A")
ORANGE     = HexColor("#F5A623")
LIME       = HexColor("#8DC63F")
LIGHT_BG   = HexColor("#F7F9F8")
GRAY_LINE  = HexColor("#DDDDDD")
GRAY_MID   = HexColor("#888888")
DARK       = HexColor("#1A1A1A")
BG_CRIT    = HexColor("#FBD9CA")
BG_MED     = HexColor("#FFF1D6")
BG_MIN     = HexColor("#E7F1D9")
BG_COMP    = HexColor("#D4EDE3")

# ── FONTS ─────────────────────────────────────────────────────────────────────
for w in ["Light","Regular","Medium","SemiBold","Bold"]:
    pdfmetrics.registerFont(TTFont(f"Lex-{w}", f"{FONTS}/Lexend-{w}.ttf"))

# ── CORE UTILS ────────────────────────────────────────────────────────────────
def F(fy, eh=0):
    """Figma/reference Y (top-down) → ReportLab Y (bottom-up)"""
    return PH - fy - eh

def tpl(c, n):
    path = f"{TPL}/tpl_page_{n}.png" if isinstance(n, int) else f"{TPL}/tpl_{n}.png"
    c.drawImage(ImageReader(path), 0, 0, PW, PH, preserveAspectRatio=False, anchor='sw')

def src(c, n):
    path = f"{SRC}/{n}.jpeg"
    if os.path.exists(path):
        c.drawImage(ImageReader(path), 0, 0, PW, PH,
                    preserveAspectRatio=False, anchor='sw')

def put(c, text, x, y_pdf, font="Lex-Regular", size=9, color=DARK):
    """Draw text at exact PDF coordinates."""
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawString(x, y_pdf, text)

def put_wrap(c, text, x, y_pdf, font="Lex-Light", size=9,
             color=DARK, max_w=220, line_h=11):
    c.setFont(font, size)
    c.setFillColor(color)
    chars = max(10, int(max_w / (size * 0.52)))
    for i, ln in enumerate(textwrap.wrap(text, chars)[:3]):
        c.drawString(x, y_pdf - i * line_h, ln)

def rbox(c, x, y_rl, w, h, fill, stroke=None, r=4, lw=0.5):
    c.setFillColor(fill)
    if stroke:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
        c.roundRect(x, y_rl, w, h, r, stroke=1, fill=1)
    else:
        c.roundRect(x, y_rl, w, h, r, stroke=0, fill=1)

def photo_crop(src_path, tmp_path, tw, th):
    try:
        img = Image.open(src_path).convert("RGB")
        iw, ih = img.size
        scale = max(tw/iw, th/ih)
        nw, nh = int(iw*scale)+1, int(ih*scale)+1
        img = img.resize((nw, nh), Image.LANCZOS)
        ox, oy = (nw-tw)//2, (nh-th)//2
        img.crop((ox, oy, ox+tw, oy+th)).save(tmp_path, "JPEG", quality=93)
        return True
    except:
        return False

# ── GENERAL INFO PAGE ─────────────────────────────────────────────────────────
# Measured from reference PDF:
#   Header green bar:  y_pdf 0–11  (22px / 2)
#   PROPERTY SNAGGING REPORT text: centered in bar
#   Logo icon: top-left ~(7, 4)
#
# Section headers (Lexend SemiBold ~12.5pt, color GREEN):
#   "General Information":    y_pdf = 66.5  → baseline ≈ 66.5 + 12 = 78.5 → draw at F(66.5, 12.5)
#   "Client Information":     y_pdf = 363.5
#   "Property Description":   y_pdf = 578.5  (note: larger ~16pt in ref)
#
# Field labels (Lexend SemiBold 10pt):
#   Row 1 labels:  y_pdf top = 111.5  → baseline ≈ F(111.5, 10) = 720.5
#   Row 2 labels:  y_pdf = 165.5
#   Row 3 labels:  y_pdf = 222.5
#   Client row 1:  y_pdf = 408.5
#   Client email:  y_pdf = 484.5
#   Prop row 1:    y_pdf = 625.0
#   Prop row 2:    y_pdf = 681.0
#   Prop row 3:    y_pdf = 737.0
#
# Values (Lexend Light 9pt):
#   Row 1 values:  y_pdf = 135.0  → RL y = F(135, 9) = 698
#   Row 2 values:  y_pdf = 192.0
#   Row 3 values:  y_pdf = 248.0
#   Client row 1:  y_pdf = 433.0
#   Client email:  y_pdf = 510.0
#   Prop row 1:    y_pdf = 645.0
#   Prop row 2:    y_pdf = 699.0
#   Prop row 3:    y_pdf = 755.0
#
# X positions:
#   Left column:   x = 53
#   Right column:  x = 311

LX = 53    # left column x
RX = 311   # right column x
LBL_SZ  = 10    # label font size (SemiBold)
VAL_SZ  = 9     # value font size (Light)
HDR_SZ  = 12.5  # section header size (SemiBold, green)
SEC_SZ  = 15.5  # "Property Description" is bigger in ref (~16pt)

def draw_general_info(c, data):
    """Only draw VALUES — new template (Untitled-2) already has all labels.
    All x/y measured pixel-by-pixel from Untitled-3 (filled reference).
    """
    tpl(c, 'gi_new')
    V = "Lex-Light"
    S = 13

    # General Information — both cols measured from ref
    put(c, data["date"],        53,  F(130.5, S), V, S)
    put(c, data["unit"],        312, F(130.5, S), V, S)

    put_wrap(c, data["project"],52,  F(184.0, S), V, S, max_w=230)
    put(c, data["type"],        312, F(185.0, S), V, S)

    put(c, data["inspector"],   53,  F(237.5, S), V, S)
    put_wrap(c, data["address"],312, F(240.0, S), V, S, max_w=230)

    # Client Information
    put(c, data["client"],      52,  F(427.0, S), V, S)
    put(c, data["reason"],      312, F(427.0, S), V, S)

    put(c, data["email"],       51,  F(503.0, S), V, S)

    # Property Description
    put(c, data["area"],        51,  F(643.0, S), V, S)
    put(c, data["furnished"],   311, F(643.0, S), V, S)

    put(c, data["floor"],       52,  F(698.0, S), V, S)
    put(c, data["year"],        314, F(698.0, S), V, S)

    put(c, data["rooms"],       53,  F(752.0, S), V, S)
    put(c, data["developer"],   312, F(752.0, S), V, S)


# ── SUMMARY PAGE ──────────────────────────────────────────────────────────────
def draw_summary(c, totals, summary_obs):
    """
    New template tpl_summary_new.png.
    Template already has: Defects found header, Critical/medium/minor labels,
    gray placeholder box. Colored boxes — we redraw with dynamic content.
    Coords measured pixel-by-pixel from ref_summary_new.png.
    """
    tpl(c, "summary_new")

    # Big total number — centered in purple box
    # Box: x_pdf=91-165 (cx=128), y_pdf=103-227
    # Text visual center y_pdf=154, font 20pt
    # ReportLab baseline = F(154+10, 0) = F(164, 0)
    # (center of glyph is ~10pt above baseline for 20pt font)
    c.setFont("Lex-SemiBold", 20)
    c.setFillColor(white)
    # x depends on digit count: 2-digit x=24, 3-digit x=16
    total_str = str(totals["total"])
    big_x = 24 if len(total_str) <= 2 else 17
    c.drawString(big_x, F(104, 20), total_str)

    # Count values — SemiBold 12pt
    # x aligned to same column (after widest label Critical ends at 168, +6pt gap = 174)
    # y matches each label baseline exactly
    for x_val, fy, cnt in [
        (176, 163.5, totals["critical"]),
        (168, 188.5, totals["medium"]),
        (160, 214.5, totals["minor"]),
    ]:
        c.setFont("Lex-SemiBold", 12)
        c.setFillColor(DARK)
        c.drawString(x_val, F(fy, 12), str(cnt))

    # Right block observations text — x=343, y_pdf=134, line_h=13pt, font 12pt Regular
    c.setFont("Lex-Regular", 12)
    c.setFillColor(DARK)
    for i, ln in enumerate(textwrap.wrap(summary_obs, 36)[:7]):
        c.drawString(343, F(134, 12) - i * 13, ln)

    # Colored boxes (Critical/Medium/Minor) — static in template, do NOT touch


# ── AREA PAGE BACKGROUND ──────────────────────────────────────────────────────
COLS = [18, 210, 402]
ROW_START = 133  # first card row starts at y_pdf=133 (from tpl_area calibration)
ROW_H = 299   # card 187 + gap between rows (432-133=299 for row2 start)

def area_bg(c, num, name, is_mep=False):
    """
    Area page background using tpl_area.png as base.
    Template ALREADY HAS: header bar, area badge bg, area name text,
    right label ('Area-Specific Defects'), TOTAL OBSERVATIONS label.
    We ONLY draw: the area NUMBER inside the badge.
    Number position measured from ref: cx=40, y_pdf=75 (center of badge).
    """
    tpl(c, "area")

    # Area NUMBER in badge
    # Rule: 1-digit x=35, 2-digit x=30. y=64 always. SemiBold 16pt white.
    num_x = 35 if len(str(num)) == 1 else 30
    c.setFont("Lex-SemiBold", 16)
    c.setFillColor(white)
    c.drawString(num_x, F(64, 16), str(num))

    # Area NAME — green SemiBold, next to badge
    # x=66 (right of badge which ends ~x=62), y=64 baseline
    c.setFont("Lex-SemiBold", 16)
    c.setFillColor(GREEN)
    c.drawString(70, F(64, 16), name)


def obs_box(c, text):
    """
    Total Observations text drawn below TOTAL OBSERVATIONS label.
    Template already has the label at y_pdf=722-732.
    Text content starts at y_pdf=742, x_pdf=24, Lex-Regular 9pt, line_h=12.
    """
    c.setFont("Lex-Regular", 9)
    c.setFillColor(DARK)
    for i, ln in enumerate(textwrap.wrap(text, 92)[:5]):
        c.drawString(24, F(742, 9) - i * 12, ln)


# ── DEFECT CARD ───────────────────────────────────────────────────────────────
# New template tpl_area.png calibrated dimensions:
# Row1 y_pdf=133-320 (h=187pt), Row2 y_pdf=432-619 (h=187pt)
# Caption text below card: Row1 y_pdf=350, Row2 y_pdf=649
# Total Observations text: y_pdf=742
# Card dimensions measured exactly from Untitled-3 card templates:
CW        = 176  # card width pt
CH        = 251  # card height pt  (176x251 = original Figma size)
BADGE_H   = 20   # badge top strip (measured: 40px/2 = 20pt)
PHOTO_H   = 167  # photo area height (40px gap to 374px = 167pt)
CAPTION_H = 64   # caption area (374px to 502px = 64pt)

# Exact badge colors measured from card templates (Untitled-3 PDFs)
BADGE_COLORS = {
    "critical":  (HexColor("#E30613"), "CRITICAL"),
    "medium":    (HexColor("#F59E00"), "MEDIUM"),
    "minor":     (HexColor("#AABF2F"), "MINOR"),
    "compliant": (HexColor("#007E50"), "COMPLIANT"),
}

def defect_card(c, fx, fy, sev, desc, photo=None):
    """
    Card layout measured from Untitled-3 card templates:
      Total:   176x251pt
      Badge:   top 20pt — colored, rounded top corners r=6, square bottom
      Photo:   20pt-187pt — bg=#DEDEDE (222,222,222)
      Caption: 187pt-251pt — white bg, text centered
    Badge text: Lex-SemiBold 8pt white, centered, baseline at y=+13 from card top
    Caption text: Lex-SemiBold 9pt dark, centered, baseline at y=+215 from card top
    """
    card_rl = F(fy, CH)   # bottom of card in RL coords

    # ── Full card bg (white) + border
    c.setFillColor(white)
    c.setStrokeColor(HexColor("#CCCCCC"))
    c.setLineWidth(0.5)
    c.roundRect(fx, card_rl, CW, CH, 6, stroke=1, fill=1)

    # ── Badge (top 20pt, rounded top, square bottom)
    col, lbl = BADGE_COLORS.get(sev, (GRAY_LINE, sev.upper()))
    badge_rl = card_rl + CH - BADGE_H   # top of badge in RL
    c.setFillColor(col)
    # Full rounded rect for badge
    c.roundRect(fx, badge_rl, CW, BADGE_H, 6, stroke=0, fill=1)
    # Square off the bottom half of badge (so bottom corners are square)
    c.rect(fx, badge_rl, CW, BADGE_H//2, stroke=0, fill=1)
    # Badge label text: SemiBold 8pt, white, centered
    c.setFont("Lex-SemiBold", 8)
    c.setFillColor(white)
    # baseline = card_rl + CH - BADGE_H + 13  (y=13 from badge top = y=+13 from card top)
    c.drawCentredString(fx + CW/2, card_rl + CH - BADGE_H + 13 - 8, lbl)

    # ── Photo area (167pt, bg=#DEDEDE)
    photo_rl = card_rl + CAPTION_H   # photo sits above caption
    c.setFillColor(HexColor("#DEDEDE"))
    c.rect(fx, photo_rl, CW, PHOTO_H, stroke=0, fill=1)
    if photo and os.path.exists(photo):
        tmp = f"/tmp/c_{fx}_{abs(int(fy))}.jpg"
        if photo_crop(photo, tmp, int(CW*4), int(PHOTO_H*4)):
            c.drawImage(ImageReader(tmp), fx, photo_rl, CW, PHOTO_H)
        else:
            c.setFont("Lex-Light", 8)
            c.setFillColor(HexColor("#999999"))
            c.drawCentredString(fx + CW/2, photo_rl + PHOTO_H/2 - 4, "placeholder")

    # ── Caption (64pt, white)
    c.setFillColor(white)
    c.rect(fx, card_rl, CW, CAPTION_H, stroke=0, fill=1)
    # Caption text: SemiBold 9pt, centered
    # From template: baseline at y=215 from card top → from card_rl: +215-CH+9 = +(215-251+9)=-27... 
    # Actually: baseline y_rl = card_rl + (CAPTION_H - 9) / 2 + 2  (vertically centered)
    c.setFont("Lex-SemiBold", 9)
    c.setFillColor(DARK)
    lines = textwrap.wrap(desc, 24)[:3]
    line_h = 11
    total_h = len(lines) * line_h
    start_y = card_rl + (CAPTION_H + total_h) / 2 - line_h + 2
    for i, ln in enumerate(lines):
        c.drawCentredString(fx + CW/2, start_y - i*line_h, ln)

def _photo_ph(c, fx, py):
    c.setFillColor(HexColor("#F0F4F2"))
    c.rect(fx, py, CW, PHOTO_H, stroke=0, fill=1)
    c.setFont("Lex-Light", 8)
    c.setFillColor(HexColor("#AAAAAA"))
    c.drawCentredString(fx+CW/2, py + PHOTO_H/2 - 4, "[ Photo ]")

def compliance_card(c, fx, fy, desc):
    """Same layout as defect_card but green COMPLIANT badge + #DEDEDE photo area."""
    card_rl = F(fy, CH)

    c.setFillColor(white)
    c.setStrokeColor(HexColor("#CCCCCC"))
    c.setLineWidth(0.5)
    c.roundRect(fx, card_rl, CW, CH, 6, stroke=1, fill=1)

    # Green badge
    badge_rl = card_rl + CH - BADGE_H
    c.setFillColor(GREEN)
    c.roundRect(fx, badge_rl, CW, BADGE_H, 6, stroke=0, fill=1)
    c.rect(fx, badge_rl, CW, BADGE_H//2, stroke=0, fill=1)
    c.setFont("Lex-SemiBold", 8)
    c.setFillColor(white)
    c.drawCentredString(fx + CW/2, card_rl + CH - BADGE_H + 13 - 8, "COMPLIANT")

    # Photo area — same gray bg as defect card
    photo_rl = card_rl + CAPTION_H
    c.setFillColor(HexColor("#DEDEDE"))
    c.rect(fx, photo_rl, CW, PHOTO_H, stroke=0, fill=1)
    # Show placeholder text
    c.setFont("Lex-Light", 8)
    c.setFillColor(HexColor("#999999"))
    c.drawCentredString(fx + CW/2, photo_rl + PHOTO_H/2 - 4, "placeholder")

    # Caption
    c.setFillColor(white)
    c.rect(fx, card_rl, CW, CAPTION_H, stroke=0, fill=1)
    c.setFont("Lex-SemiBold", 9)
    c.setFillColor(DARK)
    lines = textwrap.wrap(desc, 24)[:3]
    line_h = 11
    total_h = len(lines) * line_h
    start_y = card_rl + (CAPTION_H + total_h) / 2 - line_h + 2
    for i, ln in enumerate(lines):
        c.drawCentredString(fx+CW/2, start_y - i*line_h, ln)


# ── CONCLUSIONS ───────────────────────────────────────────────────────────────
def draw_conclusions(c, general_text, urgent_text):
    """
    Conclusions page using tpl_conclusions.png as base.
    Template already has: header, title, GENERAL CONDITION label,
    URGENT ACTIONS label, T&C section — all static, do not redraw.
    We only draw:
      - General condition text: x=51, y_pdf=139, Lex-Regular 10pt, line_h=13pt
      - Urgent actions text:    x=56, y_pdf=417, Lex-Regular 10pt, line_h=13pt
    Measured from ref_conclusions.png pixel diff.
    """
    tpl(c, "conclusions")

    # General condition text body (starts below GENERAL CONDITION label at y_pdf=122)
    c.setFont("Lex-Regular", 13)
    c.setFillColor(DARK)
    y0 = F(139, 13)
    for i, ln in enumerate(textwrap.wrap(general_text, 70)):
        c.drawString(51, y0 - i * 16, ln)

    # Urgent actions text body (starts below URGENT ACTIONS label at y_pdf=400)
    c.setFont("Lex-Regular", 13)
    c.setFillColor(DARK)
    y1 = F(417, 13)
    line_num = 0
    for ln in urgent_text.split("\n"):
        for wl in textwrap.wrap(ln.strip(), 70):
            c.drawString(56, y1 - line_num * 16, wl)
            line_num += 1
        if ln.strip():
            line_num += 0.5  # gap between numbered items

# ═══════════════════════════════════════════════════════════════════════════════
# REPORT DATA
# ═══════════════════════════════════════════════════════════════════════════════
DATA = {
    "date": "11.03.2026",
    "project": "Cedar at Creek Beach Building 2",
    "inspector": "Snaggit Inspector",
    "unit": "308",
    "type": "Apartment",
    "address": "Dubai Creek Harbour",
    "client": "TERMINAL24 L.L.C.-FZ",
    "reason": "Handover",
    "email": "TERMINAL24.INFO@GMAIL.COM",
    "area": "620 sq ft",
    "floor": "3",
    "rooms": "1 Bedroom",
    "furnished": "No",
    "year": "2026",
    "developer": "Emaar Properties",
}

TOTALS = {"critical":0, "medium":13, "minor":94, "total":107}

SUMMARY_OBS = (
    "Property at Cedar at Creek Beach Building 2, Unit 308 "
    "presents 57 total defects: 13 medium and 44 minor, with "
    "zero critical. MEP systems are fully operational. "
    "Corrective works required before handover acceptance."
)

AREAS = [
    {"num":"1","name":"Entrance","defects":[
        {"sev":"minor",  "desc":"Uneven sealant, clean and correct"},
        {"sev":"medium", "desc":"Door frame damage, repaint required"},
        {"sev":"medium", "desc":"Paint stains on wall near door"},
        {"sev":"minor",  "desc":"Scratch on main door surface"},
        {"sev":"minor",  "desc":"Missing socket cover plate"},
        {"sev":"medium", "desc":"Tile damage, excess grouting"},
        {"sev":"minor",  "desc":"Door stopper not attached securely"},
        {"sev":"minor",  "desc":"Gap between door frame and wall"},
        {"sev":"minor",  "desc":"Uneven wall painting at cornice"},
    ],"obs":"Multiple door finishing issues found. Main door has visible frame damage and paint stains. Sealant is uneven in several locations. Threshold tile shows excess grouting and minor chips. All defects are non-structural and should be corrected before handover acceptance."},

    {"num":"2","name":"Kitchen","defects":[
        {"sev":"minor",  "desc":"Tile grouting changed color — regrout"},
        {"sev":"medium", "desc":"Wobbling cabinet door — adjust hinge"},
        {"sev":"minor",  "desc":"Scratches on sink basin surface"},
        {"sev":"minor",  "desc":"Paint stains on kitchen wall tiles"},
        {"sev":"medium", "desc":"Wall undulations behind cabinet"},
        {"sev":"minor",  "desc":"Uneven sealant at countertop joint"},
    ],"obs":"Kitchen presents mostly minor defects. Cabinet hinges need adjustment and grout discoloration is visible on backsplash tiles. Sink basin has superficial scratches. Paint application on the wall is uneven. Sealant at countertop junction needs to be re-applied uniformly."},

    {"num":"3","name":"Living Area","defects":[
        {"sev":"minor",  "desc":"Uneven grouting at floor tile joints"},
        {"sev":"medium", "desc":"Damage to skirting board — replace"},
        {"sev":"medium", "desc":"Gap in sliding door frame — seal"},
        {"sev":"minor",  "desc":"Uneven painting near balcony door"},
        {"sev":"minor",  "desc":"Tile grouting incomplete at perimeter"},
        {"sev":"minor",  "desc":"Uneven sealant at wall-floor junction"},
    ],"obs":"Several finishing defects around the sliding balcony door and skirting area. The gap between the sliding door frame and the wall structure requires sealing. Floor tile grouting is incomplete at perimeter areas. Painting near the door frame needs correction."},

    {"num":"4","name":"Washing Machine Space","defects":[
        {"sev":"minor",  "desc":"Incomplete sealant at pipe penetrations"},
        {"sev":"minor",  "desc":"Scratches on wall — touch up paint"},
    ],"obs":"Washing machine space is in generally good condition. Only two minor defects: incomplete sealant around pipe penetrations and superficial paint scratches. Both are easy to rectify."},

    {"num":"5","name":"Bedroom","defects":[
        {"sev":"minor",  "desc":"Uneven painting on bedroom wall"},
        {"sev":"minor",  "desc":"Poor corner execution — ceiling junction"},
        {"sev":"minor",  "desc":"Poor corner execution — wardrobe to wall"},
        {"sev":"minor",  "desc":"Gap between wardrobe panel and ceiling"},
        {"sev":"minor",  "desc":"Grout missing at tile perimeter"},
        {"sev":"minor",  "desc":"Uneven sealant at window frame"},
        {"sev":"medium", "desc":"Wardrobe door misaligned — adjust hinges"},
        {"sev":"minor",  "desc":"Paint drips on skirting board"},
    ],"obs":"Multiple minor finishing defects. Corner execution at ceiling is poor in two locations. Built-in wardrobe has a misaligned door and gap between top panel and ceiling. Floor tile grouting incomplete at perimeter. Paint quality needs improvement at several points."},

    {"num":"6","name":"Bathroom","defects":[
        {"sev":"medium", "desc":"Crack in floor tile — replace tile"},
        {"sev":"medium", "desc":"Unbalanced bathroom door — re-hang"},
        {"sev":"minor",  "desc":"Poor corner execution at junctions"},
        {"sev":"minor",  "desc":"Uneven silicone around bathtub"},
        {"sev":"minor",  "desc":"Paint stains on ceiling near fan"},
        {"sev":"minor",  "desc":"Grout discoloration on floor tiles"},
    ],"obs":"Two medium defects require attention: cracked floor tile and unbalanced door. The door requires re-hanging. The cracked tile must be replaced to prevent water ingress. Minor issues include poor corner execution, uneven sealant and grout discoloration."},

    {"num":"7","name":"Balcony","defects":[
        {"sev":"minor",  "desc":"Debris to be removed from ceiling"},
        {"sev":"minor",  "desc":"Paint stains on balcony wall"},
        {"sev":"minor",  "desc":"Scratch on glass balcony door"},
    ],"obs":"Balcony is in good condition with only minor cosmetic defects. Construction debris remains on ceiling soffit. Paint stains on side wall. Glass door has a light surface scratch. All items are minor and easily rectifiable."},
]

MEP_AREAS = [
    {"num":"8","name":"Electrical","checks":[
        {"desc":"All sockets tested — working correctly"},
        {"desc":"MCB panel verified — works completed"},
        {"desc":"No heat detected on DB panel"},
        {"desc":"All lighting circuits — functional"},
        {"desc":"AC isolator switch — operational"},
    ]},
    {"num":"9","name":"HVAC","checks":[
        {"desc":"Cassette AC unit — cooling confirmed"},
        {"desc":"Ceiling space inspected — no leaks"},
        {"desc":"Exhaust fans operational — bathroom"},
        {"desc":"Fresh air intake — clear, unobstructed"},
        {"desc":"AC drain line — no blockage observed"},
    ]},
    {"num":"10","name":"Plumbing","checks":[
        {"desc":"No leakage observed — Kitchen"},
        {"desc":"Water heater operational — Kitchen"},
        {"desc":"Drainage confirmed — Kitchen"},
        {"desc":"Drainage confirmed — Washing Machine"},
        {"desc":"Drainage confirmed — Balcony"},
        {"desc":"Drainage confirmed — Bathroom shower"},
        {"desc":"Toilet flush mechanism — working"},
        {"desc":"No leakage at bathroom fixtures"},
    ]},
    {"num":"11","name":"Wall Moisture","checks":[
        {"desc":"No significant moisture detected"},
        {"desc":"Thermal scan completed — no anomalies"},
        {"desc":"Window seals intact — no water ingress"},
    ]},
]

GENERAL_COND = (
    "Overall, the property is in generally good condition per construction standards. All defects "
    "are non-critical; however, 13 medium defects require attention before handover acceptance. "
    "The unit presents 57 total defects: 13 medium and 44 minor, zero critical. Most affected "
    "areas are Entrance (9) and Bedroom (8). MEP systems — Electrical, HVAC, Plumbing — are "
    "all fully operational. Thermal moisture scan shows no signs of water ingress across any surface."
)

URGENT = (
    "1. Bathroom — Replace cracked floor tile immediately to prevent water ingress into the subfloor.\n"
    "2. Bathroom — Re-hang and balance the bathroom door; misalignment causes friction against frame.\n"
    "3. Living Area — Seal the gap between the sliding balcony door frame and the wall structure.\n"
    "4. Entrance — Repair door frame damage, reapply paint, reattach door stopper securely.\n"
    "5. Bedroom — Adjust wardrobe door hinges and seal the gap between top panel and ceiling."
)

# ═══════════════════════════════════════════════════════════════════════════════
# BUILD PDF
# ═══════════════════════════════════════════════════════════════════════════════
cv = canvas.Canvas(OUT, pagesize=(PW, PH))

# Page 1: Cover
tpl(cv, 1)
cv.showPage()

# Page 2: General Info
draw_general_info(cv, DATA)
cv.showPage()

# Page 3: Summary
draw_summary(cv, TOTALS, SUMMARY_OBS)
cv.showPage()

# Pages 4+: Area defect pages
CARDS_PER_PAGE = 6
for area in AREAS:
    defects = area["defects"]
    chunks  = [defects[i:i+CARDS_PER_PAGE] for i in range(0, len(defects), CARDS_PER_PAGE)]
    for ci, chunk in enumerate(chunks):
        area_bg(cv, area["num"], area["name"])
        for i, d in enumerate(chunk):
            fx = COLS[i % 3]
            fy = ROW_START + (i // 3) * ROW_H
            defect_card(cv, fx, fy, d["sev"], d["desc"], photo=d.get("photo"))
        if ci == len(chunks)-1:
            obs_box(cv, area["obs"])
        cv.showPage()

# MEP pages — max 6 cards per page (2 rows x 3 cols)
for area in MEP_AREAS:
    defects = area.get("defects", area.get("checks", []))
    chunks = [defects[i:i+CARDS_PER_PAGE] for i in range(0, len(defects), CARDS_PER_PAGE)]
    for ci, chunk in enumerate(chunks):
        area_bg(cv, area["num"], area["name"])
        for i, d in enumerate(chunk):
            fx = COLS[i % 3]
            fy = ROW_START + (i // 3) * ROW_H
            sev = d.get("sev", "compliant")
            desc = d.get("desc", d.get("description", ""))
            photo = d.get("photo")
            defect_card(cv, fx, fy, sev, desc, photo=photo)
        if ci == len(chunks) - 1:
            obs_box(cv, area.get("obs", "All MEP systems inspected."))
        cv.showPage()

# Conclusions
draw_conclusions(cv, GENERAL_COND, URGENT)
cv.showPage()

# Final page (template)
tpl(cv, 4)
cv.showPage()

cv.save()
print(f"✅  {OUT}")
