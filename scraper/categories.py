"""Category definitions and matching.

Categories:
  - air-cooled-911-family  (bespoke predicate; generation regex needs special care)
  - 60s-muscle             }
  - 80s-90s-japanese       }  declarative specs (makes + tokens + year + exclusions)
  - vintage-trucks         }
  - german-wagons          }

A category is a predicate over a *normalized* record (see parse.parse_item). Matching
favors recall (under-exclude) but gates on make + a model/body token + year, then drops
obvious non-car lots (parts, engine-only, rollers, replicas, scale models).

Spec category-id list and token sets were researched per category; see
~/.gstack/projects/BAT-Scanner design notes. Tweak the lists below to taste.
"""

from __future__ import annotations

import re

CATEGORY_AIR_COOLED_911 = "air-cooled-911-family"

# --- air-cooled 911 family (bespoke; 996+ is water-cooled and post-1998) ------------

# Allow a trailing letter (912E, 911S, 911T, 911SC) but not another digit. A leading
# \b keeps "930" out of "1930".
_GENERATION_RE = re.compile(r"\b(911|912|930|964|993)(?![0-9])")
_AIRCOOLED_MIN_YEAR = 1964
_AIRCOOLED_MAX_YEAR = 1998

# Phrase-based, not bare nouns (bare "engine"/"parts"/"shell" wrongly drop real cars).
_AIRCOOLED_EXCLUSIONS = [
    r"\bgo[\s-]?kart\b", r"\bkart\b", r"\bparts\b", r"\bpart[\s-]?out\b",
    r"\bengine\s+(?:only|lot)\b", r"\b(?:complete|spare)\s+engine\b",
    r"\bgearbox\s+only\b", r"\btransaxle\s+only\b", r"\broller\b",
    r"\breplica\b", r"\btribute\b", r"\bre[\s-]?creation\b", r"\brecreation\b",
    r"\bbody\s+shell\b", r"\brolling\s+shell\b",
]
_AIRCOOLED_EXCLUSION_RE = re.compile("|".join(_AIRCOOLED_EXCLUSIONS), re.IGNORECASE)


def _make_is_porsche(record: dict) -> bool:
    make = record.get("make") or {}
    return (make.get("name") or "").strip().lower() == "porsche" or \
           (make.get("slug") or "").strip().lower() == "porsche"


def matches_air_cooled_911(record: dict) -> bool:
    if not _make_is_porsche(record):
        return False
    year = record.get("year")
    if not isinstance(year, int) or not (_AIRCOOLED_MIN_YEAR <= year <= _AIRCOOLED_MAX_YEAR):
        return False
    title = record.get("title") or ""
    if not _GENERATION_RE.search(title):
        return False
    if _AIRCOOLED_EXCLUSION_RE.search(title):
        return False
    return True


# --- declarative spec categories ----------------------------------------------------

def _compile_tokens(tokens):
    """Compile a token list into a word-ish alternation regex.

    Boundaries use "not adjacent to an alphanumeric" lookarounds so tokens with
    punctuation ("R/T", "'Cuda", "4-4-2", "GT-R", "F-100") and bare numbers ("442",
    "300ZX") match as whole words without matching inside larger numbers/years.
    """
    toks = sorted({t.strip() for t in tokens if t and t.strip()}, key=len, reverse=True)
    if not toks:
        return None
    alt = "|".join(re.escape(t) for t in toks)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _make_spec(category_id, label, *, makes, model_tokens, body_keywords,
               require_model, require_body, min_year, max_year, exclusions,
               scoped_tokens=None):
    """scoped_tokens maps a make-name (lowercase) -> tokens that count ONLY for that
    make. Use it for tokens that collide across makes (e.g. Chevy "K20" truck vs the
    Honda "K20" engine in a swap title)."""
    scoped = {}
    for mk, toks in (scoped_tokens or {}).items():
        cre = _compile_tokens(toks)
        if cre:
            scoped[mk.strip().lower()] = cre
    return {
        "id": category_id,
        "label": label,
        "makes": {m.strip().lower() for m in makes},
        "tokens_re": _compile_tokens(list(model_tokens) + list(body_keywords)),
        "body_re": _compile_tokens(body_keywords),
        "scoped_res": scoped,
        "require_model": require_model,
        "require_body": require_body,
        "min_year": min_year,
        "max_year": max_year,
        "exclusion_re": _compile_tokens(exclusions),
    }


def _spec_predicate(spec):
    def pred(record: dict) -> bool:
        make = record.get("make") or {}
        name = (make.get("name") or "").strip().lower()
        slug = (make.get("slug") or "").strip().lower()
        if spec["makes"] and name not in spec["makes"] and slug not in spec["makes"]:
            return False
        year = record.get("year")
        if spec["min_year"] is not None or spec["max_year"] is not None:
            if not isinstance(year, int):
                return False
            if spec["min_year"] is not None and year < spec["min_year"]:
                return False
            if spec["max_year"] is not None and year > spec["max_year"]:
                return False
        title = record.get("title") or ""
        if spec["exclusion_re"] and spec["exclusion_re"].search(title):
            return False
        if spec["require_body"] and (spec["body_re"] is None or not spec["body_re"].search(title)):
            return False
        if spec["require_model"]:
            ok = bool(spec["tokens_re"] and spec["tokens_re"].search(title))
            if not ok:
                sre = spec["scoped_res"].get(name) or spec["scoped_res"].get(slug)
                ok = bool(sre and sre.search(title))
            if not ok:
                return False
        return True
    return pred


_SPECS = [
    _make_spec(
        "60s-muscle", "60s Muscle",
        makes=["Chevrolet", "Ford", "Pontiac", "Dodge", "Plymouth", "Buick",
               "Oldsmobile", "AMC", "Mercury", "Shelby"],
        model_tokens=[
            "Mustang", "Boss 302", "Boss 429", "Mach 1", "GT350", "GT500", "GT-350",
            "GT-500", "Shelby", "Camaro", "Z/28", "Z28", "Chevelle", "Malibu SS",
            "El Camino SS", "Nova SS", "Chevy II", "Impala SS", "Biscayne", "Yenko",
            "COPO", "GTO", "Judge", "Firebird", "Trans Am", "Charger", "Daytona",
            "Super Bee", "Coronet R/T", "Coronet Super Bee", "Dart GTS", "Dart GT",
            "Demon", "Challenger", "R/T", "Road Runner", "Roadrunner", "Superbird",
            "GTX", "Barracuda", "Cuda", "'Cuda", "Duster", "Sport Fury", "442", "4-4-2",
            "Cutlass S", "Cutlass Supreme", "W-30", "Hurst/Olds", "Hurst Olds",
            "Rallye 350", "Gran Sport", "GS 350", "GS 400", "GS 455", "GSX",
            "Skylark GS", "Wildcat", "AMX", "Javelin", "Rebel Machine", "SC/Rambler",
            "Torino", "Torino GT", "Torino Cobra", "Talladega", "Fairlane",
            "Fairlane GT", "Cobra Jet", "Cyclone", "Cyclone GT", "Cyclone Spoiler",
            "Comet", "Marauder", "SS 396", "SS 454", "SS396", "SS454", "SS 350", "SS"],
        body_keywords=[],
        require_model=True, require_body=False, min_year=1964, max_year=1972,
        exclusions=[
            "replica", "tribute", "clone", "recreation", "re-creation", "kit car",
            "go-kart", "go kart", "pedal car", "parts", "parts lot", "parts car",
            "project shell", "body shell", "body only", "no engine", "rolling chassis",
            "fiberglass body", "engine only", "for parts", "Mustang II", "Pinto",
            "Maverick", "Falcon", "Ranchero", "Nomad", "Greenbrier", "Corvair", "Vega",
            "tractor", "golf cart", "die-cast", "diecast", "scale model", "slot car",
            "Power Wheels", "Corvette"],
    ),
    _make_spec(
        "80s-90s-japanese", "80s-90s Japanese",
        makes=["Toyota", "Lexus", "Nissan", "Datsun", "Honda", "Acura", "Mazda",
               "Mitsubishi", "Subaru", "Suzuki", "Isuzu"],
        model_tokens=[
            "Supra", "MR2", "MR-2", "AE86", "Corolla GT-S", "Corolla GTS", "Levin",
            "Trueno", "Celica", "Celica GT-Four", "Celica All-Trac", "RX-7", "RX7",
            "FD", "FC", "MX-5", "Miata", "NSX", "Skyline", "GT-R", "GTR", "GTS-R",
            "300ZX", "280ZX", "240Z", "260Z", "280Z", "300Z", "Fairlady", "240SX",
            "180SX", "200SX", "Silvia", "Pulsar", "Sentra SE-R", "SE-R", "CRX",
            "Civic Si", "Civic Type R", "Civic SiR", "Integra", "Type R", "Prelude",
            "Beat", "3000GT", "GTO", "Eclipse", "Starion", "Cordia", "Lancer Evolution",
            "Lancer Evo", "Evolution", "FTO", "WRX", "STI", "Impreza", "Legacy", "SVX",
            "Land Cruiser", "FJ40", "FJ60", "FJ62", "FJ80", "4Runner", "Cappuccino",
            "Samurai", "Soarer", "Chaser", "Cressida", "Crown", "Stagea", "Cosmo",
            "RX-3", "RX-2", "Galant VR-4", "VR-4", "Delica", "Impulse", "VehiCROSS",
            "SC300", "SC400", "Pao", "Figaro", "S-Cargo", "Pikes Peak"],
        body_keywords=[],
        require_model=True, require_body=False, min_year=1980, max_year=1999,
        exclusions=[
            "go-kart", "go kart", "kart", "parts lot", "parts car", "part-out",
            "part out", "engine only", "engine lot", "complete engine", "spare engine",
            "transmission only", "gearbox only", "transaxle only", "roller",
            "rolling shell", "body shell", "replica", "tribute", "re-creation",
            "recreation", "1/18 scale", "1/24 scale", "scale model", "pedal car",
            "slot car", "die-cast", "diecast"],
    ),
    _make_spec(
        "vintage-trucks", "Vintage Trucks & SUVs",
        makes=["Ford", "Chevrolet", "GMC", "Dodge", "Toyota", "Land Rover", "Jeep",
               "International", "International Harvester", "Datsun", "Nissan", "Willys"],
        model_tokens=[
            # Unambiguous nameplates safe across all truck makes.
            "F-100", "F100", "F-250", "F250", "F-350", "F350", "F-Series", "Bronco",
            "Ranchero", "Courier", "Blazer", "Suburban", "Jimmy", "Power Wagon",
            "Power-Wagon", "Ramcharger", "Town Wagon", "Land Cruiser", "Land-Cruiser",
            "FJ40", "FJ-40", "FJ43", "FJ45", "FJ55", "FJ60", "FJ62", "FJ80", "BJ40",
            "BJ42", "Hilux", "Hi-Lux", "4Runner", "Defender", "Wagoneer",
            "Grand Wagoneer", "Gladiator", "Scrambler", "CJ", "CJ-2A", "CJ-3A", "CJ-3B",
            "CJ-5", "CJ-6", "CJ-7", "CJ-8", "Comanche", "Scout", "Scout II", "Scout 80",
            "Scout 800", "Hardbody", "Jeepster", "Commando"],
        body_keywords=["Pickup", "Pick-Up", "Truck", "Stepside", "Fleetside",
                       "Stakeside", "Flatbed"],
        # Collision-prone tokens scoped to their make (e.g. "K20" the Chevy truck vs
        # "K20" the Honda swap engine; bare "88"/"109"/"720"/"Series").
        scoped_tokens={
            "chevrolet": ["C10", "C-10", "C20", "C-20", "K10", "K-10", "K20", "K-20",
                          "C/K", "C/K10", "K5", "Apache", "3100", "3600", "3800"],
            "gmc": ["C10", "C-10", "C20", "C-20", "K10", "K-10", "K20", "K-20", "C/K",
                    "C/K10", "K5", "Sierra"],
            "dodge": ["D100", "D-100", "D150", "D-150", "D200", "D-200", "D250",
                      "W100", "W150", "W200", "W250"],
            "ford": ["F-1"],
            "land rover": ["Series", "Series I", "Series II", "Series IIA",
                           "Series III", "88", "109"],
            "datsun": ["620", "520", "720"],
            "nissan": ["620", "520", "720"],
            "international": ["Travelall", "Travelette", "Terra", "Traveler",
                             "FC-150", "FC-170"],
            "international harvester": ["Travelall", "Travelette", "Terra", "Traveler",
                                        "FC-150", "FC-170"],
            "jeep": ["J10", "J-10", "J20", "J-20"],
            "willys": ["MB"],
        },
        require_model=True, require_body=False, min_year=1940, max_year=1995,
        exclusions=[
            "go-kart", "go kart", "gokart", "parts lot", "parts truck", "part-out",
            "part out", "engine only", "engine lot", "complete engine",
            "transmission only", "axle only", "roller", "rolling chassis", "body shell",
            "rolling shell", "replica", "tribute", "re-creation", "recreation",
            "scale model", "pedal car", "toy", "model kit", "frame only", "cab only",
            "bed only", "project shell", "monster truck", "1/18", "1/24", "1:18",
            "1:24", "for parts"],
    ),
    _make_spec(
        "german-wagons", "German Wagons",
        makes=["Mercedes-Benz", "BMW", "Audi", "Volkswagen", "Porsche", "Opel"],
        model_tokens=[
            "Avant", "Touring", "Variant", "T-Modell", "Shooting Brake", "Kombi",
            "RS6", "RS4", "RS2", "S4 Avant", "S6 Avant", "A4 Avant", "A6 Avant",
            "A4 Allroad", "A6 Allroad", "Allroad", "300TD", "300TE", "230TE", "240TD",
            "280TE", "320TE", "E320 Wagon", "E55 Wagon", "E63 Wagon", "Passat Variant",
            "Jetta Wagon", "Jetta SportWagen", "Golf SportWagen", "Golf Variant",
            "Passat Wagon", "M5 Touring", "M3 Touring", "Panamera Sport Turismo",
            "Taycan Cross Turismo", "Taycan Sport Turismo", "CLS Shooting Brake",
            "Caravan", "Astra Caravan", "Kadett Caravan"],
        body_keywords=[
            "Wagon", "Estate", "Avant", "Touring", "Variant", "T-Modell", "T Modell",
            "T-Wagon", "Shooting Brake", "Kombi", "SportWagen", "Sport Turismo",
            "Cross Turismo", "Caravan", "Allroad", "Sportwagon",
            # Mercedes T-codes ARE the wagon signal (T = T-Modell/estate; the sedan is
            # "300D"/"300E"), so they count as body evidence even with no "Wagon" word.
            "300TD", "300TE", "230TE", "240TD", "280TE", "320TE"],
        require_model=False, require_body=True, min_year=1965, max_year=2026,
        exclusions=[
            "Gran Turismo", "5 Series GT", "5-Series GT", "3 Series GT", "3-Series GT",
            "Gran Coupe", "sedan", "go-kart", "replica", "tribute", "project shell",
            "parts car", "parts only", "body shell", "pedal car", "scale model",
            "brochure", "Volvo", "Saab", "Touring Superleggera",
            # kill the Porsche 911/GT3 "Touring" (a coupe, not a wagon) false positive
            "911", "GT3"],
    ),
]

_SPEC_PREDICATES = {s["id"]: _spec_predicate(s) for s in _SPECS}

# Registry: id -> predicate. Air-cooled first (bespoke), then the spec categories.
CATEGORY_PREDICATES = {CATEGORY_AIR_COOLED_911: matches_air_cooled_911, **_SPEC_PREDICATES}
CATEGORY_IDS = tuple(CATEGORY_PREDICATES.keys())
CATEGORY_LABELS = {CATEGORY_AIR_COOLED_911: "Air-Cooled 911 Family",
                   **{s["id"]: s["label"] for s in _SPECS}}


def match_categories(record: dict, only: list[str] | None = None) -> list[str]:
    """Return the ids of every category this record belongs to."""
    ids = []
    for cid, predicate in CATEGORY_PREDICATES.items():
        if only is not None and cid not in only:
            continue
        try:
            if predicate(record):
                ids.append(cid)
        except Exception:
            continue
    return ids
