# Snaggit AI — v4

Property snagging Telegram bot for the Dubai market, built from v3 with one key change: **per-defect AI classification removed**.

## What changed from v3

Inspectors now set severity and description **manually** for every defect:

```
Inspector sends photo → Tap severity button → Type description → Saved
```

There is **no AI analysis of individual photos** and **no 303-type defect classifier**. Inspectors decide what's critical, medium, or minor — the bot just captures their judgment.

## Where AI still runs

AI is used **only during PDF generation**, in a single batched call to Claude Sonnet 4, to write professional narrative text:

| PDF block | Source |
|---|---|
| Summary page — observations (right column) | AI: `summary_obs` |
| Conclusions page — general condition | AI: `general_condition` |
| Conclusions page — urgent items | AI: `urgent` |
| **Total Observations** (every zone page) | **AI: `zone_obs[zone_number]`** |

All four are returned as JSON in one request. If the AI call fails, the bot falls back to hardcoded templates — the PDF still generates.

## Architecture (unchanged from v3)

- **Multi-inspector** via 6-char join code
- **Zones defined upfront** by the lead inspector
- **Zone locking** via `assigned_to`
- **State persisted** in Supabase (3 tables: `inspections`, `inspection_zones`, `inspection_members`)
- **PDF generation** via `generate_v5_newtempl.py` (template injection)
- **PDF delivered to all members** via Telegram with retry

## Conversation states

29 states (v3 had 30 — `AI_CONFIRM` removed).

Defect sub-flow:
```
DEFECT_PHOTO → DEFECT_SEVERITY → DEFECT_DESC → AFTER_DEFECT
```

## Environment variables (Railway)

```
BOT_TOKEN          # NEW token — create a new bot in BotFather for v4
ANTHROPIC_KEY      # Same or new, needed for PDF text generation
SUPABASE_URL       # Decide: new project or shared with v3
SUPABASE_KEY       # Service role key (not anon)
REPORT_DIR=/app/data
ASSETS_DIR=/app
```

## Deployment

1. Create new GitHub repo `vladtolmaci/snaggitai-v4`, push these files
2. New Railway project → connect GitHub → auto-deploy
3. Set env vars above in Railway Raw Editor
4. Create new bot with BotFather, copy token to `BOT_TOKEN`

## Files

- `bot.py` — main bot (1697 lines, down from v3's 2320)
- `generate_v5_newtempl.py` — PDF generator (unchanged from v3)
- `tpl_v2/` — PDF template images (unchanged from v3)
- `fonts/` — Lex fonts (unchanged from v3)
- `Dockerfile` — Python 3.11 slim (unchanged from v3)
- `requirements.txt` — `python-telegram-bot==21.9`, `httpx`, `supabase`, `Pillow`, `reportlab`
