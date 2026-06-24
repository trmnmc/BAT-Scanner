"""Canonical vehicle-identity tests (no network).

The core regression: a multiword model must never collapse to its first word — El Camino is
not "el", Land Cruiser is not "land", Grand Wagoneer is not "grand" — because that fragment
pollutes the comp pool. Make detection reuses scraper.parse so these read like real titles.
"""

from scraper import identity, parse


def _record(title, year=None, id=1, url="https://bringatrailer.com/listing/x/"):
    make, models, _ = parse.derive_make_models(title)
    if year is None:
        year = parse.parse_year({"title": title})
    return {"id": id, "title": title, "year": year, "make": make, "models": models,
            "listing_url": url}


def _ident(title, **kw):
    return identity.derive_identity(_record(title, **kw))


# --- the six required cases ---------------------------------------------------------------

def test_el_camino_is_not_model_el():
    vi = _ident("1972 Chevrolet El Camino SS")
    assert vi["canonical_make"] == "chevrolet"
    assert vi["canonical_model"] == "el-camino"      # NOT "el"
    assert vi["confidence"] == "high"


def test_land_cruiser_is_not_model_land():
    vi = _ident("1978 Toyota Land Cruiser FJ40")
    assert vi["canonical_make"] == "toyota"
    assert vi["canonical_model"] == "land-cruiser"   # NOT "land"
    assert vi["confidence"] == "high"


def test_grand_wagoneer_is_not_model_grand():
    vi = _ident("1989 Jeep Grand Wagoneer")
    assert vi["canonical_make"] == "jeep"
    assert vi["canonical_model"] == "grand-wagoneer"  # NOT "grand"
    assert vi["confidence"] == "high"
    # the plain Wagoneer and the Grand Cherokee remain distinct, not collapsed to "grand"
    assert identity.derive_identity(_record("1985 Jeep Wagoneer"))["canonical_model"] == "wagoneer"
    assert identity.derive_identity(_record("1995 Jeep Grand Cherokee"))["canonical_model"] == "grand-cherokee"


def test_alfa_romeo_gtv():
    vi = _ident("1969 Alfa Romeo GTV 1750")
    assert vi["canonical_make"] == "alfa-romeo"       # multiword make handled by parse
    assert vi["canonical_model"] == "gtv"
    assert vi["confidence"] == "high"
    # GTV6 stays distinct from GTV
    assert identity.derive_identity(_record("1986 Alfa Romeo GTV6"))["canonical_model"] == "gtv6"


def test_mercedes_benz_models():
    # number + body code, both joined ("350SL") and split ("300 SL")
    assert _ident("1972 Mercedes-Benz 350SL")["canonical_model"] == "350sl"
    assert _ident("1985 Mercedes-Benz 300 SL")["canonical_model"] == "300sl"
    assert _ident("1991 Mercedes-Benz 560 SEL")["canonical_model"] == "560sel"
    assert _ident("1972 Mercedes-Benz 350SL")["canonical_make"] == "mercedes-benz"
    # a bare displacement number with NO body code is genuinely ambiguous -> low confidence
    bare = _ident("1980 Mercedes-Benz 300")
    assert bare["confidence"] == "low"
    assert any("body code" in r for r in bare["ambiguity_reasons"])


def test_porsche_911_variants():
    carrera = _ident("1985 Porsche 911 Carrera Coupe")
    assert carrera["canonical_make"] == "porsche" and carrera["canonical_model"] == "911"
    assert carrera["trim"] == "carrera" and carrera["body_style"] == "coupe"
    assert carrera["confidence"] == "high"
    # generation chassis codes keep their own identity (a 930 Turbo is not a base 911 for comps)
    turbo = _ident("1976 Porsche 930 Turbo")
    assert turbo["canonical_model"] == "930" and turbo["chassis_code"] == "930"
    assert turbo["trim"] == "turbo" and turbo["generation"].startswith("930")
    assert _ident("1995 Porsche 993 Carrera")["canonical_model"] == "993"
    assert _ident("1973 Porsche 911 Targa")["canonical_model"] == "911"


# --- the collision safety net (unregistered multiword models) -----------------------------

def test_unregistered_collision_prefix_is_low_confidence_not_silently_chopped():
    # Chrysler isn't in the registry, so "Grand Voyager" can't be recognized as a full model.
    # It must NOT silently become model "grand" — it is flagged low-confidence instead.
    vi = _ident("1994 Chrysler Grand Voyager")
    assert vi["confidence"] == "low"
    assert identity.is_low_confidence(vi) is True
    assert any("truncated multiword" in r for r in vi["ambiguity_reasons"])

    # a normal single-token model on an unregistered make stays usable (high confidence)
    accord = _ident("2005 Honda Accord")
    assert accord["canonical_model"] == "accord" and accord["confidence"] == "high"


# --- manual overrides ---------------------------------------------------------------------

def test_override_by_auction_key_and_url():
    overrides = {
        "bat:555": {"canonical_make": "ferrari", "canonical_model": "250-gto"},
        "https://bringatrailer.com/listing/weird-build/": {"canonical_model": "el-camino"},
    }
    rec = _record("1962 Mystery Coupe", id=555, url="https://x/")
    vi = identity.derive_identity(rec, overrides)
    assert vi["canonical_model"] == "250-gto" and vi["canonical_make"] == "ferrari"
    assert vi["manually_overridden"] is True and vi["source"] == "override"
    assert vi["confidence"] == "high"

    rec2 = _record("1972 Unknown Thing", id=999, url="https://bringatrailer.com/listing/weird-build/")
    vi2 = identity.derive_identity(rec2, overrides)
    assert vi2["canonical_model"] == "el-camino" and vi2["manually_overridden"] is True


def test_override_clears_a_collision_ambiguity_reason():
    overrides = {"bat:7": {"canonical_model": "grand-voyager"}}
    rec = _record("1994 Chrysler Grand Voyager", id=7)
    vi = identity.derive_identity(rec, overrides)
    assert vi["canonical_model"] == "grand-voyager"
    assert vi["confidence"] == "high"
    assert not any("truncated multiword" in r for r in vi["ambiguity_reasons"])


def test_load_overrides_handles_missing_and_corrupt(tmp_path):
    assert identity.load_overrides(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert identity.load_overrides(str(bad)) == {}
    good = tmp_path / "ov.json"
    good.write_text('{"overrides": {"bat:1": {"canonical_model": "x"}}}', encoding="utf-8")
    assert identity.load_overrides(str(good)) == {"bat:1": {"canonical_model": "x"}}


# --- the shipped overrides file is well-formed --------------------------------------------

def test_shipped_overrides_file_is_valid():
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "data" / "identity_overrides.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(doc.get("overrides"), dict)             # present and a map (may be empty)
    assert identity.load_overrides(str(p)) == doc["overrides"]


# --- comp annotation + canonical matching -------------------------------------------------

def test_annotate_comp_adds_canonical_from_title_keeps_legacy():
    comp = {"id": 1, "title": "1972 Chevrolet El Camino SS", "make": "chevrolet", "model": "el",
            "year": 1972, "price": 30000, "listing_url": "https://bringatrailer.com/listing/ec/"}
    ann = identity.annotate_comp(comp)
    assert ann["make"] == "chevrolet" and ann["model"] == "el"      # legacy fields untouched
    assert ann["canonical_model"] == "el-camino"                    # canonical fixed from title
    assert ann["listing_url"].endswith("/ec/")


def test_comp_match_reasons_and_similarity():
    car_make, car_model, car_year = "chevrolet", "el-camino", 1972
    good = {"id": 1, "title": "1971 Chevrolet El Camino", "make": "chevrolet", "model": "el", "year": 1971}
    m = identity.comp_match(car_make, car_model, car_year, good, year_band=3)
    assert m["matched"] is True and 0.7 <= m["similarity"] <= 1.0
    assert any("same make+model" in r for r in m["reasons"])

    # a different "grand"-prefixed model must NOT match (the whole point of canonical matching)
    other = {"id": 2, "title": "1989 Jeep Grand Wagoneer", "make": "jeep", "model": "grand", "year": 1972}
    m2 = identity.comp_match(car_make, car_model, car_year, other, year_band=3)
    assert m2["matched"] is False
    assert any("make differs" in r or "model differs" in r for r in m2["reasons"])

    # same model but outside the year band -> not matched, with an explicit reason (never silent)
    far = {"id": 3, "title": "1995 Chevrolet El Camino", "make": "chevrolet", "model": "el", "year": 1995}
    m3 = identity.comp_match(car_make, car_model, car_year, far, year_band=3)
    assert m3["matched"] is False and any("year gap" in r for r in m3["reasons"])


# --- review fixes ------------------------------------------------------------------------

def test_descriptive_only_override_does_not_unsuppress_a_chopped_identity():
    # a trim-only override applies the trim but must NOT un-suppress a still-chopped canonical_model
    vi = identity.derive_identity(_record("1994 Chrysler Grand Voyager", id=9), {"bat:9": {"trim": "limited"}})
    assert vi["trim"] == "limited" and vi["manually_overridden"] is True
    assert vi["canonical_model"] == "grand"        # still a chopped fragment
    assert vi["confidence"] == "low" and identity.is_low_confidence(vi) is True
    # only a canonical_model override actually resolves the identity
    vi2 = identity.derive_identity(_record("1994 Chrysler Grand Voyager", id=9),
                                   {"bat:9": {"canonical_model": "grand-voyager"}})
    assert vi2["canonical_model"] == "grand-voyager" and vi2["confidence"] == "high"


def test_annotate_comp_excludes_chopped_fragments_from_the_pool():
    chopped = identity.annotate_comp({"id": 1, "title": "1994 Chrysler Grand Voyager",
                                      "make": "chrysler", "model": "grand", "year": 1994, "price": 9000})
    assert chopped["canonical_model"] is None              # "grand" never becomes matchable
    assert identity.comp_canonical(chopped) == ("chrysler", None)
    good = identity.annotate_comp({"id": 2, "title": "1972 Chevrolet El Camino", "make": "chevrolet",
                                   "model": "el", "year": 1972, "price": 30000})
    assert good["canonical_model"] == "el-camino"
    # a title-less comp keeps its legacy model slug (nothing to derive, nothing chopped)
    titleless = identity.annotate_comp({"id": 3, "make": "porsche", "model": "911", "year": 1985, "price": 70000})
    assert titleless["canonical_model"] == "911"


def test_comp_canonical_trusts_annotation_without_re_deriving():
    # an annotated comp (has canonical_make) is trusted verbatim — even a deliberately-None model
    annotated = {"id": 1, "title": "1994 Chrysler Grand Voyager", "canonical_make": "chrysler",
                 "canonical_model": None, "make": "chrysler", "model": "grand"}
    assert identity.comp_canonical(annotated) == ("chrysler", None)


def test_trans_am_unifies_with_firebird():
    assert identity.derive_identity(_record("1979 Pontiac Trans Am"))["canonical_model"] == "firebird"
    assert identity.derive_identity(_record("1979 Pontiac Firebird Trans Am"))["canonical_model"] == "firebird"


def test_car_canonical_falls_back_to_legacy_when_no_identity():
    rec = {"make": {"slug": "porsche"}, "models": [{"slug": "911"}]}
    assert identity.car_canonical(rec) == ("porsche", "911")
    rec2 = {"make": {"slug": "x"}, "models": [{"slug": "y"}],
            "vehicle_identity": {"canonical_make": "porsche", "canonical_model": "930"}}
    assert identity.car_canonical(rec2) == ("porsche", "930")
