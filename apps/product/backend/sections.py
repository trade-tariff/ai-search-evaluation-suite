"""UK Online Tariff section mapping.

The HS / UK-Global-Tariff hierarchy has 21 top-level sections, each covering
a range of 2-digit chapters. Stratifying accuracy by section (rather than
ad-hoc categories) lines up our evaluation with how the OTT actually
presents goods to traders: https://www.trade-tariff.service.gov.uk/browse

Aligns with WCO HS: same section numbers and chapter ranges.
"""

from __future__ import annotations

# Each entry: (section_number, roman_numeral, title, (chapter_lo, chapter_hi))
# Chapter ranges are inclusive. Chapter 77 is reserved (no goods).
_SECTIONS: list[tuple[int, str, str, tuple[int, int]]] = [
    (1,  "I",     "Live animals; animal products",                                   (1, 5)),
    (2,  "II",    "Vegetable products",                                               (6, 14)),
    (3,  "III",   "Animal or vegetable fats and oils",                                (15, 15)),
    (4,  "IV",    "Prepared foodstuffs; beverages, spirits, tobacco",                 (16, 24)),
    (5,  "V",     "Mineral products",                                                 (25, 27)),
    (6,  "VI",    "Products of the chemical or allied industries",                    (28, 38)),
    (7,  "VII",   "Plastics and articles thereof; rubber and articles thereof",       (39, 40)),
    (8,  "VIII",  "Raw hides, skins, leather, furskins; saddlery; travel goods",      (41, 43)),
    (9,  "IX",    "Wood and articles of wood; wood charcoal; cork; basketware",       (44, 46)),
    (10, "X",     "Pulp of wood; paper and paperboard; printed books",                (47, 49)),
    (11, "XI",    "Textiles and textile articles",                                    (50, 63)),
    (12, "XII",   "Footwear, headgear, umbrellas, feathers, artificial flowers",      (64, 67)),
    (13, "XIII",  "Articles of stone, plaster, cement, asbestos, ceramic, glass",     (68, 70)),
    (14, "XIV",   "Natural or cultured pearls, precious stones, metals, coins",       (71, 71)),
    (15, "XV",    "Base metals and articles of base metal",                           (72, 83)),
    (16, "XVI",   "Machinery and mechanical appliances; electrical equipment",        (84, 85)),
    (17, "XVII",  "Vehicles, aircraft, vessels and associated transport equipment",   (86, 89)),
    (18, "XVIII", "Optical, photographic, medical instruments; clocks; musical",      (90, 92)),
    (19, "XIX",   "Arms and ammunition; parts and accessories",                       (93, 93)),
    (20, "XX",    "Miscellaneous manufactured articles",                              (94, 96)),
    (21, "XXI",   "Works of art, collectors' pieces and antiques",                    (97, 97)),
]


def section_for_chapter(chapter: int) -> dict | None:
    for num, rom, title, (lo, hi) in _SECTIONS:
        if lo <= chapter <= hi:
            return {"number": num, "roman": rom, "title": title}
    return None


def section_for_code(commodity_code: str | None) -> dict | None:
    """Return section metadata for a commodity code string, or None if unparseable."""
    if not commodity_code:
        return None
    code = commodity_code.strip()
    if len(code) < 2 or not code[:2].isdigit():
        return None
    try:
        chapter = int(code[:2])
    except ValueError:
        return None
    return section_for_chapter(chapter)


def all_sections() -> list[dict]:
    """Full section list for UI dropdowns."""
    return [
        {"number": num, "roman": rom, "title": title,
         "chapter_range": f"{lo:02d}-{hi:02d}" if lo != hi else f"{lo:02d}"}
        for num, rom, title, (lo, hi) in _SECTIONS
    ]
