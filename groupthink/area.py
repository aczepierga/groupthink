"""Plan area: which offer cities count as 'in' the plan's city. Other cities match exactly only."""

CITY_ALIASES = {"New York": ["New York City", "NYC", "Manhattan"]}
SAME_AREA = {
    "Chicago": ["Evanston", "Oak Park", "Skokie", "Cicero", "Berwyn"],
    "Austin": ["Round Rock", "Cedar Park", "Pflugerville", "Leander", "Georgetown", "Lakeway", "Bee Cave",
               "Buda", "Kyle", "West Lake Hills"],
    "New York": ["Brooklyn", "Queens", "Bronx", "Staten Island", "Long Island City", "Astoria", "Flushing",
                 "Jackson Heights", "Harlem", "Williamsburg", "Jersey City", "Hoboken"],
}


def city_key(s: str) -> str:
    return (s or "").split(",")[0].strip().casefold()


def area_keys(plan_city: str) -> set[str]:
    k = city_key(plan_city)
    keys = {k}
    for table in (CITY_ALIASES, SAME_AREA):
        for name, towns in table.items():
            if city_key(name) == k:
                keys.update(city_key(t) for t in towns)
    return keys


def in_area(plan_city: str, offer_city: str) -> bool:
    k = city_key(offer_city)
    return bool(k) and k in area_keys(plan_city)


def area_location(plan_city: str, hit: dict) -> dict | None:
    """The first location of the hit that is in the plan's area, else None."""
    keys = area_keys(plan_city)
    for loc in hit.get("locations") or []:
        k = city_key(loc.get("city") or "")
        if k and k in keys:
            return loc
    return None
