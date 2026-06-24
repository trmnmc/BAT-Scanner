"""scraper/identity.py — canonical vehicle identity.

Turns a parsed auction/comp record into a richer, MATCH-SAFE ``vehicle_identity``: it
recognizes multiword models (El Camino, Land Cruiser, Grand Wagoneer) so a broken model
fragment ("El" / "Land" / "Grand") can never pollute the comp pool, and it assigns a
``confidence`` so a shaky identity SUPPRESSES a trusted valuation instead of inventing one.

Design rules (CLAUDE.md + the Stage 6A spec):
  - DATA-DRIVEN: model knowledge lives in the REGISTRY / tables below, not in scattered
    one-off ``if`` branches sprinkled through the pipeline.
  - ADDITIVE: the legacy ``make`` / ``models`` fields are NOT touched; this only ADDS a
    ``vehicle_identity`` block (Stage-1-compatible, validated as optional).
  - SAFE-BY-DEFAULT: an unrecognized multiword model — a *bare collision prefix* such as
    "grand" with nothing the registry knows after it — is flagged low-confidence, so the
    valuation is suppressed rather than silently comped against every "grand" on the board.
  - NO NETWORK, NO AI: pure title parsing plus a small local overrides file.

The block fields (all optional; absent values are ``None`` / ``[]``):
    canonical_make, canonical_model, generation, chassis_code, trim, body_style, engine,
    transmission, drivetrain, market, originality, confidence, ambiguity_reasons, source,
    manually_overridden
"""

from __future__ import annotations

import json
import os
import re

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _norm_token(t: str) -> str:
    return (t or "").strip().strip(",").lower()


# make-slug aliases -> canonical make slug (so "Mercedes" and "Mercedes-Benz" comp together).
MAKE_ALIASES = {
    "mercedes": "mercedes-benz", "mercedes-benz": "mercedes-benz", "benz": "mercedes-benz",
    "chevy": "chevrolet", "vw": "volkswagen", "alfa": "alfa-romeo", "rolls": "rolls-royce",
    "range-rover": "land-rover", "landrover": "land-rover", "aston": "aston-martin",
}

# Body-style and trim/market keyword tables (best-effort; a miss is None, never invented).
_BODY_STYLES = [
    ("convertible", ["convertible", "cabriolet", "cabrio", "drophead", "spyder", "spider", "roadster"]),
    ("targa", ["targa"]),
    ("wagon", ["wagon", "estate", "avant", "touring", "variant", "shooting-brake", "shooting brake"]),
    ("coupe", ["coupe", "coupé", "berlinetta", "fastback", "hardtop", "liftback"]),
    ("sedan", ["sedan", "saloon", "berlina"]),
    ("hatchback", ["hatchback", "hatch"]),
    ("suv", ["suv"]),
    ("pickup", ["pickup", "pick-up"]),
    ("ute", ["el camino", "ranchero"]),
]
_TRIMS = [
    "carrera", "turbo", "targa", "gts", "gt3", "gt2", "gt4", "gt", "rs", "gtr", "gti", "gtv6",
    "type-r", "type r", "si", "ss", "z28", "zl1", "amg", "quadrifoglio", "abarth", "cosworth",
    "shelby", "cobra", "hemi", "scat-pack", "denali", "raptor", "trd", "sport", "touring", "base",
    "trans am",
]
_MARKETS = [
    ("jdm", ["jdm", "right-hand drive", "rhd", "right hand drive"]),
    ("euro", ["euro", "european-market", "european market"]),
    ("usdm", ["usdm", "us-market", "us market"]),
]
_TRANSMISSIONS = [
    ("manual", [r"\bmanual\b", r"\b\d-speed\b", r"\bfive-speed\b", r"\bsix-speed\b", r"\bfour-speed\b",
                r"\bstick\b", r"\bg50\b", r"\b915\b"]),
    ("automatic", [r"\bautomatic\b", r"\bauto\b", r"\btiptronic\b", r"\bpdk\b", r"\bslushbox\b"]),
]
_DRIVETRAINS = [
    ("awd", [r"\bawd\b", r"\ball-wheel drive\b", r"\bquattro\b", r"\b4matic\b", r"\bsh-awd\b"]),
    ("4wd", [r"\b4wd\b", r"\b4x4\b", r"\bfour-wheel drive\b"]),
    ("rwd", [r"\brwd\b", r"\brear-wheel drive\b"]),
    ("fwd", [r"\bfwd\b", r"\bfront-wheel drive\b"]),
]
_ENGINES = [
    ("v12", [r"\bv-?12\b", r"\btwelve-cylinder\b"]),
    ("v10", [r"\bv-?10\b"]),
    ("v8", [r"\bv-?8\b"]),
    ("v6", [r"\bv-?6\b"]),
    ("flat-6", [r"\bflat-?six\b", r"\bflat-?6\b"]),
    ("inline-6", [r"\binline-?six\b", r"\binline-?6\b", r"\bstraight-?six\b", r"\bstraight-?6\b"]),
    ("electric", [r"\belectric\b", r"\bev\b"]),
]
_ORIGINALITY = [
    ("modified", [r"\bmodified\b", r"\brestomod\b", r"\bresto-mod\b", r"\bcustom\b", r"-powered\b",
                  r"\bswapped\b", r"\bwidebody\b"]),
    ("replica", [r"\breplica\b", r"\btribute\b", r"\brecreation\b", r"\bclone\b", r"\bkit\b"]),
    ("restored", [r"\brestored\b", r"\brestoration\b"]),
    ("original", [r"\boriginal\b", r"\bsurvivor\b", r"\bnumbers-matching\b", r"\bunrestored\b"]),
]

# Porsche 911 generation chassis codes -> a friendly generation label.
_P911_CHASSIS = {
    "901": "1965-1973 (O-series)", "911": "911", "930": "930 (1975-1989 Turbo/SC/Carrera)",
    "964": "964 (1989-1994)", "993": "993 (1994-1998, last air-cooled)",
    "996": "996 (1999-2004)", "997": "997 (2005-2012)", "991": "991 (2012-2019)", "992": "992 (2019-)",
}

# Mercedes-Benz body codes that follow the displacement number (e.g. 300 SL, 560 SEL, 450 SLC).
_MB_BODY_CODES = {
    "sl", "slc", "sel", "se", "sec", "cl", "clk", "cls", "sls", "amg", "td", "d", "e", "te",
    "ce", "c", "ge", "gd", "g", "ml", "gl", "gls", "glc", "gle", "glk", "slk", "slr", "s",
}

# ---------------------------------------------------------------------------
# Per-make model REGISTRY. `models` lists known model NAMES (lowercase, space-separated),
# matched LONGEST-FIRST against the model phrase so "grand wagoneer" wins over "wagoneer".
# `pattern` triggers a make-specific rule. This is the single place model knowledge lives.
# ---------------------------------------------------------------------------
REGISTRY = {
    "chevrolet": {"models": ["el camino", "monte carlo", "chevy ii", "ss 396", "bel air",
                              "c10", "k10", "k5 blazer", "grand sport"]},
    "gmc": {"models": ["jimmy", "syclone", "typhoon", "sierra grande"]},
    "toyota": {"models": ["land cruiser", "fj cruiser", "fj40", "fj60", "fj62", "hilux", "land-cruiser"]},
    "jeep": {"models": ["grand wagoneer", "grand cherokee", "wagoneer", "cherokee", "cj-5", "cj-7",
                         "cj5", "cj7", "scrambler", "gladiator"]},
    "buick": {"models": ["grand national", "gran sport", "gnx", "regal grand national", "riviera"]},
    "pontiac": {"models": ["grand prix", "grand am", "grand ville", "trans am", "gto", "firebird"]},
    "ford": {"models": ["grand torino", "country squire", "model a", "model t", "gran torino"]},
    "lincoln": {"models": ["town car", "continental", "mark iv", "mark v"]},
    "mercury": {"models": ["grand marquis", "marquis", "cougar"]},
    "oldsmobile": {"models": ["cutlass supreme", "cutlass", "442", "vista cruiser"]},
    "land-rover": {"models": ["range rover", "range rover classic", "defender", "discovery",
                              "series iii", "series ii", "series i"]},
    "mercedes-benz": {"pattern": "mb"},
    "porsche": {"pattern": "porsche",
                "models": ["911", "912", "914", "356", "928", "944", "924", "968", "718",
                           "boxster", "cayman", "cayenne", "macan", "panamera", "taycan", "carrera gt"]},
    "alfa-romeo": {"models": ["giulia", "giulietta", "gtv6", "gtv", "gt junior", "spider", "montreal",
                              "alfetta", "duetto", "4c", "8c", "stelvio"]},
    "bmw": {"models": ["3.0 cs", "3.0 csl", "2002", "2002 tii", "m3", "m5", "m6", "m2", "1602",
                       "z3", "z4", "z8", "i8"]},
    "datsun": {"models": ["240z", "260z", "280z", "510", "620", "1600", "2000", "fairlady"]},
    "nissan": {"models": ["skyline", "skyline gt-r", "gt-r", "300zx", "350z", "370z", "silvia"]},
    "mazda": {"models": ["rx-7", "rx7", "rx-8", "miata", "mx-5", "cosmo"]},
    "subaru": {"models": ["wrx", "wrx sti", "sti", "brz", "svx", "brat"]},
}

# Model aliases: a sub-model/trim that is commonly titled on its own but is really a variant of a
# parent model — map it to the PARENT's canonical so the two don't fragment into separate comp pools
# ("Trans Am" and "Firebird Trans Am" are the same car for comps). Matched longest-first, anchored at
# the start of the model phrase, BEFORE the per-make model list. Data-driven, in one place.
MODEL_ALIASES = {
    "pontiac": {"trans am": "firebird"},
}

# Collision prefixes: words that are essentially NEVER a complete model on their own — a bare
# model token equal to one of these (when no full multiword model was recognized) is a strong
# "we chopped a multiword model" signal, so the identity is flagged low-confidence.
#
# This set is deliberately CONSERVATIVE and curated, NOT auto-derived from every multiword model's
# first word: words like "regal" (Regal / Regal Grand National), "sierra" (Sierra / Sierra Grande),
# "gt" (Ford GT / GT Junior) or "super" (Super Beetle) ARE valid standalone models, and flagging
# them would wrongly suppress common cars. Registered multiword models (e.g. "grand wagoneer")
# resolve via the registry BEFORE this net is consulted, so it only catches UNREGISTERED truncations.
COLLISION_PREFIXES = {"el", "land", "grand", "gran", "monte", "santa"}


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------
_OVERRIDE_FIELDS = ("canonical_make", "canonical_model", "generation", "chassis_code", "trim",
                    "body_style", "engine", "transmission", "drivetrain", "market", "originality")


def load_overrides(path: str) -> dict:
    """Load data/identity_overrides.json -> {stable_key: {field: value, ...}}. Missing/corrupt
    file -> {} (overrides are a convenience; a bad file must never break the pipeline)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    ov = doc.get("overrides") if isinstance(doc, dict) else None
    return ov if isinstance(ov, dict) else {}


def _override_for(record: dict, overrides: dict):
    """Match an override by bat auction key ("bat:<id>") or exact listing_url. Returns the
    override dict (whitelisted to identity fields) or None."""
    if not overrides:
        return None
    rid = record.get("id")
    keys = []
    if rid is not None:
        keys.append(f"bat:{rid}")
        keys.append(str(rid))
    url = record.get("listing_url")
    if isinstance(url, str) and url:
        keys.append(url)
        keys.append(url.rstrip("/"))
    for k in keys:
        ov = overrides.get(k)
        if isinstance(ov, dict):
            return {f: ov[f] for f in _OVERRIDE_FIELDS if f in ov}
    return None


# ---------------------------------------------------------------------------
# title -> model phrase
# ---------------------------------------------------------------------------
def model_phrase(title: str, make_name: str) -> list:
    """Return the model-portion tokens of a title: everything after the year and the make.

    Uses the ALREADY-PARSED make name to peel the right number of make words (1 for
    "Chevrolet", 2 for "Alfa Romeo"), so we never re-detect the make. Original casing is kept.
    """
    title = title or ""
    m = _YEAR_RE.search(title)
    after = title[m.end():].strip() if m else title.strip()
    tokens = after.split()
    make_words = len((make_name or "").split())
    return tokens[make_words:] if make_words and len(tokens) >= make_words else tokens


def _match_first(text: str, table) -> str | None:
    low = text.lower()
    for label, needles in table:
        for n in needles:
            if n.startswith(r"\b") or "\\" in n:
                if re.search(n, low):
                    return label
            elif n in low:
                return label
    return None


def _detect_trim(model_tokens_norm) -> str | None:
    joined = " ".join(model_tokens_norm)
    for t in _TRIMS:
        if re.search(r"\b" + re.escape(t) + r"\b", joined):
            return t
    return None


# ---------------------------------------------------------------------------
# model resolution
# ---------------------------------------------------------------------------
def _resolve_model(make_slug: str, tokens_norm: list):
    """Return (canonical_model, chassis_code, generation, reasons, matched_kind).

    matched_kind: "registry" | "pattern" | "single" | "none". reasons explains ambiguity.
    """
    reasons: list = []
    spec = REGISTRY.get(make_slug, {})

    # 0. model aliases (a sub-model titled on its own -> its parent's canonical, e.g. Trans Am -> firebird)
    aliases = MODEL_ALIASES.get(make_slug, {})
    for phrase in sorted(aliases, key=lambda p: -len(p.split())):
        pw = phrase.split()
        if tokens_norm[:len(pw)] == pw:
            return aliases[phrase], None, None, reasons, "registry"

    # 1. longest known multiword/model-name match
    for name in sorted(spec.get("models", []), key=lambda n: -len(n.split())):
        nw = name.split()
        if tokens_norm[:len(nw)] == nw:
            return slugify(name), None, None, reasons, "registry"

    # 2. per-make systematic patterns
    pat = spec.get("pattern")
    if pat == "porsche" and tokens_norm:
        head = tokens_norm[0]
        if head in _P911_CHASSIS:      # 911 + the 901/930/964/993/996/997/991/992 generations
            # keep the generation as its own canonical model (a 930 Turbo is not a base 911 for
            # comp purposes); record the chassis/generation as finer signals.
            chassis = None if head == "911" else head
            gen = None if head == "911" else _P911_CHASSIS[head]
            return head, chassis, gen, reasons, "pattern"
        # other Porsche single tokens fall through to single-token handling
    if pat == "mb" and tokens_norm:
        head = tokens_norm[0]
        m = re.match(r"^(\d{2,3})([a-z]{1,4})$", head)               # joined, e.g. "350sl"
        if m and m.group(2) in _MB_BODY_CODES:
            return f"{m.group(1)}{m.group(2)}", None, None, reasons, "pattern"
        if re.match(r"^\d{2,3}$", head):                            # split, e.g. "300 sl"
            nxt = tokens_norm[1] if len(tokens_norm) > 1 else None
            if nxt in _MB_BODY_CODES:
                return f"{head}{nxt}", None, None, reasons, "pattern"
            reasons.append(f"Mercedes model number '{head}' without a body code (300 vs 300SL/300SEL ambiguous)")
            return head, None, None, reasons, "ambiguous-number"

    # 3. single-token fallback
    if tokens_norm:
        head = tokens_norm[0]
        if head in COLLISION_PREFIXES:
            reasons.append(f"possible truncated multiword model: '{head}' "
                           f"(model word matches a known multiword prefix but no full model was recognized)")
            return slugify(head), None, None, reasons, "collision"
        return slugify(head), None, None, reasons, "single"

    reasons.append("no model token found in title")
    return None, None, None, reasons, "none"


# ---------------------------------------------------------------------------
# the public entry point
# ---------------------------------------------------------------------------
def _empty_identity() -> dict:
    return {
        "canonical_make": None, "canonical_model": None, "generation": None, "chassis_code": None,
        "trim": None, "body_style": None, "engine": None, "transmission": None, "drivetrain": None,
        "market": None, "originality": None, "confidence": CONF_LOW, "ambiguity_reasons": [],
        "source": "title", "manually_overridden": False,
    }


def derive_identity(record: dict, overrides: dict | None = None) -> dict:
    """Build the vehicle_identity block for a parsed record (live auction or comp).

    Reads only already-present fields (title, make, models, year, id, listing_url) plus an
    optional overrides map. NEVER fetches anything. Returns the full block; absent values are
    None / [] and a shaky identity carries confidence="low" with ambiguity_reasons.
    """
    ident = _empty_identity()
    title = record.get("title") or ""
    make = record.get("make") or {}
    make_name = make.get("name")
    make_slug = make.get("slug")
    canonical_make = MAKE_ALIASES.get(make_slug, make_slug) if make_slug else None
    ident["canonical_make"] = canonical_make

    tokens = model_phrase(title, make_name)
    tokens_norm = [t for t in (_norm_token(x) for x in tokens) if t]

    canonical_model, chassis, generation, reasons, kind = _resolve_model(canonical_make, tokens_norm)
    ident["canonical_model"] = canonical_model
    ident["chassis_code"] = chassis
    ident["generation"] = generation
    ident["ambiguity_reasons"] = list(reasons)

    # best-effort descriptive fields from the whole title (None on a miss, never invented).
    ident["trim"] = _detect_trim(tokens_norm)
    ident["body_style"] = _match_first(title, _BODY_STYLES)
    ident["engine"] = _match_first(title, _ENGINES)
    ident["transmission"] = _match_first(title, _TRANSMISSIONS)
    ident["drivetrain"] = _match_first(title, _DRIVETRAINS)
    ident["market"] = _match_first(title, _MARKETS)
    ident["originality"] = _match_first(title, _ORIGINALITY)

    # confidence from how cleanly make+model resolved. LOW (a chopped multiword, a bare Mercedes
    # number, or no model at all) is the signal that suppresses a trusted valuation downstream.
    year = record.get("year")
    has_year = isinstance(year, int)
    if kind in ("collision", "ambiguous-number") or not canonical_model:
        ident["confidence"] = CONF_LOW
    elif not canonical_make:
        ident["confidence"] = CONF_MEDIUM
        ident["ambiguity_reasons"].append("make not recognized")
    else:                                  # kind in ("registry", "pattern", "single")
        ident["confidence"] = CONF_HIGH if has_year else CONF_MEDIUM

    # manual override: apply the supplied fields. But ONLY a human-supplied canonical_model proves
    # the IDENTITY is resolved — a descriptive-only override (trim/body/market/…) must NOT un-suppress
    # a car whose canonical_model is still a chopped fragment. So confidence is raised to high (and the
    # truncation/ambiguity reasons cleared) ONLY when the override actually supplies canonical_model.
    ov = _override_for(record, overrides or {})
    if ov:
        for f, v in ov.items():
            ident[f] = v
        ident["manually_overridden"] = True
        ident["source"] = "override"
        if "canonical_model" in ov:
            ident["confidence"] = CONF_HIGH
            ident["ambiguity_reasons"] = [r for r in (ident.get("ambiguity_reasons") or [])
                                          if not r.startswith("possible truncated")
                                          and not r.startswith("Mercedes model number")]
    return ident


def is_low_confidence(ident: dict | None) -> bool:
    """A low-confidence identity must SUPPRESS a trusted valuation (Stage 6A task 10)."""
    if not isinstance(ident, dict):
        return False
    return ident.get("confidence") == CONF_LOW


def is_known_model(make_slug: str, model_slug: str) -> bool:
    """True when (make, model) is an AUTHORITATIVE registry/pattern model — a 911 trim variant or
    an "el-camino", not a single-token fallback. The identity report uses this to tell a benign
    multi-trim bucket (authoritative) from a real collision (a registry gap / chopped model)."""
    if not model_slug:
        return False
    spec = REGISTRY.get(make_slug, {})
    if model_slug in {slugify(n) for n in spec.get("models", [])}:
        return True
    if make_slug == "porsche" and model_slug in _P911_CHASSIS:
        return True
    if make_slug == "mercedes-benz" and re.match(r"^\d{2,3}[a-z]{1,4}$", model_slug):
        return True
    return False


# ---------------------------------------------------------------------------
# comp annotation + canonical matching (used by value.py + the report)
# ---------------------------------------------------------------------------
def annotate_comp(comp: dict, overrides: dict | None = None) -> dict:
    """Return a COPY of a comp with canonical identity fields added (derived from its title).

    Keeps every legacy field; adds canonical_make / canonical_model / generation / trim /
    body_style / transmission and a stable listing_url when present. No network (title only).
    """
    if not isinstance(comp, dict):
        return comp
    out = dict(comp)
    # comps store make/model as bare slugs and (usually) a title. Build a record-shaped view.
    view = {"title": comp.get("title", ""), "year": comp.get("year"), "id": comp.get("id"),
            "listing_url": comp.get("listing_url"),
            "make": {"name": comp.get("make"), "slug": comp.get("make")}}
    ident = derive_identity(view, overrides)
    out.setdefault("listing_url", comp.get("listing_url"))
    out["canonical_make"] = ident["canonical_make"] or comp.get("make")
    # A LOW-confidence identity with a model is a CHOPPED fragment ("grand" from Grand Voyager, a
    # bare Mercedes "300"). Do NOT promote it — or the legacy slug, which is the SAME fragment — to a
    # matchable canonical_model, or it would re-pollute the comp pool (the exact thing Stage 6A stops).
    # Leave it None so matching skips it, mirroring the car-side valuation suppression.
    cm = ident["canonical_model"]
    if is_low_confidence(ident) and cm is not None:
        cm = None
    elif cm is None:
        cm = comp.get("model")          # no canonical derived (e.g. a title-less comp) -> keep legacy slug
    out["canonical_model"] = cm
    for f in ("generation", "trim", "body_style", "transmission"):
        if ident.get(f) is not None:
            out[f] = ident[f]
    return out


def car_canonical(record: dict) -> tuple:
    """(canonical_make, canonical_model) for a live record — prefers its vehicle_identity,
    falls back to legacy make/model slugs so an identity-less record still matches as before."""
    vi = record.get("vehicle_identity") or {}
    cm = vi.get("canonical_make") or (record.get("make") or {}).get("slug")
    cmod = vi.get("canonical_model")
    if cmod is None:
        models = record.get("models") or []
        cmod = models[0].get("slug") if models else None
    return cm, cmod


def comp_canonical(comp: dict) -> tuple:
    """(canonical_make, canonical_model) for a comp. If the comp was already annotated (the pipeline
    annotates the whole pool ONCE), TRUST that result — including a deliberately-None canonical_model
    for a chopped fragment — instead of re-deriving per car. Otherwise derive from the title, else
    fall back to the comp's legacy make/model slugs."""
    if "canonical_make" in comp:                      # annotation ran -> trust it, never re-derive
        return comp.get("canonical_make"), comp.get("canonical_model")
    if comp.get("title"):
        ann = annotate_comp(comp)
        return ann.get("canonical_make"), ann.get("canonical_model")
    return comp.get("make"), comp.get("model")


def comp_match(car_make, car_model, car_year, comp, *, year_band) -> dict:
    """Score one comp against a car identity. Returns {matched, similarity, reasons}.

    similarity in [0,1]: make+model are required for a match; year-closeness fills the rest.
    `reasons` ALWAYS explains the outcome (matched or not) — there is no silent acceptance or
    rejection, and there is NEVER a fall-through to a broad-category median.
    """
    reasons: list = []
    cmake, cmodel = comp_canonical(comp)
    cyear = comp.get("year")

    if not (car_make and car_model):
        return {"matched": False, "similarity": 0.0,
                "reasons": ["car identity is incomplete (no canonical make/model)"]}
    if cmake != car_make:
        return {"matched": False, "similarity": 0.0,
                "reasons": [f"make differs: {cmake!r} != {car_make!r}"]}
    if cmodel != car_model:
        return {"matched": False, "similarity": 0.0,
                "reasons": [f"model differs: {cmodel!r} != {car_model!r}"]}
    reasons.append(f"same make+model: {car_make}/{car_model}")

    if not isinstance(cyear, int) or not isinstance(car_year, int):
        return {"matched": False, "similarity": 0.4, "reasons": reasons + ["comp or car has no year"]}
    dy = abs(cyear - car_year)
    if dy > year_band:
        return {"matched": False, "similarity": round(0.5 * max(0.0, 1 - dy / 25.0), 3),
                "reasons": reasons + [f"year gap {dy} > band {year_band} ({cyear} vs {car_year})"]}
    sim = round(0.7 + 0.3 * (1 - dy / max(1, year_band)), 3)
    reasons.append(f"year within band: {cyear} vs {car_year} (Δ{dy} ≤ {year_band})")
    return {"matched": True, "similarity": sim, "reasons": reasons}
