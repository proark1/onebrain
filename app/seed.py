"""Sample NFT Gym data across every tier.

Seeded on first run so the UI isn't empty and you can immediately switch roles
and watch what's answerable change. Each tuple is
(title, classification, location, category, text).
"""

from __future__ import annotations

from app.ingest.pipeline import IngestPipeline

SAMPLE_DOCS = [
    ("Opening hours & locations", "public", "global", "general",
     "NFT Gym Munich is open Monday to Friday 06:00 to 23:00, and Saturday and Sunday "
     "08:00 to 20:00. NFT Gym Berlin is open daily 07:00 to 22:00. On public holidays all "
     "locations follow Sunday hours."),
    ("Weekly class schedule", "public", "global", "general",
     "Muay Thai runs Monday, Wednesday and Friday at 18:00. Brazilian Jiu-Jitsu runs Tuesday "
     "and Thursday at 19:00 and Saturday at 11:00. Boxing runs Monday to Thursday at 07:00 and "
     "20:00. Kids classes run Saturday at 10:00."),
    ("Membership plans & pricing", "public", "global", "general",
     "The Flex membership is 49 EUR per month with no commitment. The Standard membership is 39 "
     "EUR per month on a 12-month term. The Student rate is 29 EUR per month with valid ID. A "
     "single day pass is 15 EUR."),
    ("Refund & cancellation SOP", "internal", "munich", "cs",
     "Members may cancel with one month notice before the end of their term. Refunds for unused "
     "prepaid months are issued to the original payment method within 14 days. Escalate any "
     "refund dispute above 200 EUR to the location manager."),
    ("Munich front-desk opening checklist", "internal", "munich", "ops",
     "Unlock the doors at 05:45 and disarm the alarm using the code kept in the safe. Switch on "
     "the mat lighting, check the sauna temperature, count the till float of 150 EUR, and confirm "
     "the day's class list on the board."),
    ("Berlin equipment maintenance log", "internal", "berlin", "ops",
     "Heavy bags are inspected monthly for stitching wear. Cage mats are disinfected nightly. "
     "Treadmill 3 is out of service pending a belt replacement ordered on 14 June 2026."),
    ("Trainer salary bands 2026", "restricted", "global", "hr",
     "The junior trainer band is 2,600 to 3,100 EUR per month. The senior trainer band is 3,400 "
     "to 4,200 EUR per month. The head coach band is 4,500 to 5,500 EUR per month. Bonuses are "
     "capped at 8 percent of base salary."),
    ("Disciplinary process guidelines", "restricted", "global", "hr",
     "Step one is a documented verbal warning. Step two is a written warning retained for 12 "
     "months. Step three may lead to termination and must be reviewed with HR and the works "
     "council before any decision."),
    ("Q1 2026 revenue by location", "confidential", "global", "finance",
     "Munich Q1 revenue was 214,000 EUR. Berlin Q1 revenue was 168,500 EUR. Blended gross margin "
     "was 61 percent. Advertising spend was 22,000 EUR against an 18,000 EUR budget."),
    ("Q1 ad campaign performance", "confidential", "global", "marketing",
     "The Fight Ready Instagram campaign drove 1,240 trial signups at a 12 EUR cost per lead. "
     "Google Search brought 430 signups at 19 EUR per lead. Retargeting had the best return on "
     "ad spend at 4.1."),
]


def seed_if_empty(pipeline: IngestPipeline, store) -> int:
    if store.count() > 0:
        return 0
    for title, classification, location, category, text in SAMPLE_DOCS:
        pipeline.ingest_text(
            title=title, text=text, classification=classification,
            location=location, category=category, uploaded_by="seed",
        )
    return len(SAMPLE_DOCS)
