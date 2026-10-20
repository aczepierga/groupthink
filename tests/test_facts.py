import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from groupthink.facts import Facts, extract_facts

FIX = Path(__file__).parent / "fixtures"


def load_offers():
    return [json.loads(line) for line in (FIX / "agent_offers.jsonl").read_text().splitlines()]


def test_extract_limit():
    f = extract_facts(["Limit 1 per person, may buy 2 additional as gifts."])
    assert f.limit_per_person == 1
    assert f.gift_ok is True


def test_extract_limit_variants():
    assert extract_facts(["Limit 3 per person(s), may buy 3 additional as gift(s)."]).limit_per_person == 3
    assert extract_facts(["Limit one redemption per person."]).limit_per_person == 1
    assert extract_facts(["Limit 2 per person. Limit 1 per person."]).limit_per_person == 1
    assert extract_facts(["Limit 1 per table."]).limit_per_person is None


def test_extract_appointment():
    assert extract_facts(["Appointment/Reservation required. Merchant contact information."]).appointment is True
    assert extract_facts(["Reservations are strictly mandatory."]).appointment is True
    # "No booking required" must not read as an appointment requirement
    assert extract_facts(["No booking required, present your purchase on arrival."]).appointment is False
    assert extract_facts(["Reservation not required."]).appointment is False


def test_extract_21():
    assert extract_facts(["Must be 21 or older."]).age_21 is True
    assert extract_facts(["Age 21+ only."]).age_21 is True
    assert extract_facts(["Must be 18 or older."]).age_21 is False


def test_extract_other_facts():
    f = extract_facts(["New customers only.", "Not valid with other offers or promotions.",
                       "Tax and gratuity are not included.", "Valid Monday - Thursday"])
    assert f.new_customers_only is True
    assert f.cannot_combine is True
    assert f.tax_tip_extra is True
    assert f.weekday_only is True


def test_unknown_is_none():
    f = extract_facts(None)
    assert f.appointment is None
    assert f == Facts()


def test_no_match_is_false_not_none():
    f = extract_facts(["Enjoy your visit."])
    assert f.appointment is False and f.age_21 is False and f.gift_ok is False
    assert f.new_customers_only is False and f.weekday_only is False
    assert f.cannot_combine is False and f.tax_tip_extra is False
    assert f.limit_per_person is None
    empty = extract_facts([])
    assert empty.appointment is False


def test_fact_counts_on_fixture():
    offers = load_offers()
    assert len(offers) == 40
    counts = Counter()
    for o in offers:
        for k, v in asdict(extract_facts(o["finePrint"])).items():
            if v not in (None, False):
                counts[k] += 1
    # computed once from agent_offers.jsonl (40 of the 185 offers); on all 185 the same regexes give
    # limit 150, appointment 106, cannot_combine 73, gift 39, tax_tip 28, age_21 17, new 10, weekday 4
    assert dict(counts) == {
        "limit_per_person": 33, "appointment": 24, "cannot_combine": 19, "gift_ok": 7,
        "age_21": 6, "tax_tip_extra": 6, "new_customers_only": 4, "weekday_only": 1,
    }


def test_fixture_covers_every_party_bound():
    bounds = {op["partySize"]["bound"] if op.get("partySize") else None
              for o in load_offers() for op in o["options"]}
    # the 185-offer corpus holds no at_least option; that case is covered by a synthetic test in test_candidates
    assert bounds == {"exactly", "up_to", None}
