"""Fill and validate extracted address fields without guessing.

The extraction model is allowed to return a street line with missing city,
state, or country. This module turns that into a saveable address using only
trusted local data:

  1. a valid India PIN from the committed GeoNames table;
  2. the contact's own ``companyLocation`` text;
  3. city/state names visible next to the address quote on the source page.

It never invents a street line, and it never overwrites a value the page
actually stated. Conflicts are returned so the geocode gate can reject or
downgrade the address explicitly instead of saving it quietly.
"""
import re

from . import geo, pin_codes

# Common spelling variants found in cards, directories, and old pages.
_STATE_ALIASES = {
    "andaman and nicobar": "andaman & nicobar islands",
    "andaman nicobar": "andaman & nicobar islands",
    "pondicherry": "puducherry",
    "jammu and kashmir": "jammu & kashmir",
    "orissa": "odisha",
    "uttaranchal": "uttarakhand",
    "telengana": "telangana",
    "chattisgarh": "chhattisgarh",
    "new delhi": "delhi",
}

_CITY_STATE = {
    "hyderabad": "telangana", "secunderabad": "telangana",
    "warangal": "telangana", "nizamabad": "telangana",
    "vijayawada": "andhra pradesh", "visakhapatnam": "andhra pradesh",
    "vizag": "andhra pradesh", "guntur": "andhra pradesh",
    "tirupati": "andhra pradesh", "nellore": "andhra pradesh",
    "kolkata": "west bengal", "howrah": "west bengal",
    "siliguri": "west bengal", "durgapur": "west bengal",
    "asansol": "west bengal", "newtown": "west bengal",
    "new town": "west bengal", "salt lake": "west bengal",
    "rajarhat": "west bengal",
    "mumbai": "maharashtra", "bombay": "maharashtra",
    "pune": "maharashtra", "poona": "maharashtra",
    "nagpur": "maharashtra", "nashik": "maharashtra",
    "thane": "maharashtra", "navi mumbai": "maharashtra",
    "hinjewadi": "maharashtra", "pimpri": "maharashtra",
    "chennai": "tamil nadu", "madras": "tamil nadu",
    "coimbatore": "tamil nadu", "madurai": "tamil nadu",
    "bengaluru": "karnataka", "bangalore": "karnataka",
    "mysuru": "karnataka", "mysore": "karnataka",
    "mangalore": "karnataka", "hubli": "karnataka",
    "whitefield": "karnataka", "electronic city": "karnataka",
    "madhapur": "telangana", "gachibowli": "telangana",
    "kondapur": "telangana", "hitech city": "telangana",
    "delhi": "delhi", "new delhi": "delhi",
    "gurugram": "haryana", "gurgaon": "haryana",
    "faridabad": "haryana", "panipat": "haryana",
    "noida": "uttar pradesh", "ghaziabad": "uttar pradesh",
    "lucknow": "uttar pradesh", "kanpur": "uttar pradesh",
    "ahmedabad": "gujarat", "surat": "gujarat",
    "vadodara": "gujarat", "baroda": "gujarat",
    "jaipur": "rajasthan", "jodhpur": "rajasthan",
    "indore": "madhya pradesh", "bhopal": "madhya pradesh",
    "patna": "bihar", "ranchi": "jharkhand",
    "guwahati": "assam", "bhubaneswar": "odisha",
    "kochi": "kerala", "cochin": "kerala",
    "thiruvananthapuram": "kerala", "trivandrum": "kerala",
    "chandigarh": "chandigarh", "dehradun": "uttarakhand",
    "shimla": "himachal pradesh", "srinagar": "jammu & kashmir",
    "jammu": "jammu & kashmir", "panaji": "goa",
    "raipur": "chhattisgarh", "amritsar": "punjab",
    "ludhiana": "punjab",
}

_ADDRESS_KEYS = ("address_line1", "address_line2", "city", "state",
                 "postal_code", "country")


def normalize(value):
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(text.split())


def clean_fields(fields, *, india=True):
    cleaned = {key: str((fields or {}).get(key) or "").strip()
               for key in _ADDRESS_KEYS}
    for key in ("address_line1", "address_line2"):
        if cleaned[key]:
            text = re.sub(r"\s*\|\s*", ", ", cleaned[key])
            text = re.sub(r"\s*,\s*(?:,\s*)+", ", ", text)
            cleaned[key] = text.strip(" ,")
    if not india:
        return cleaned
    raw_pin = cleaned.get("postal_code") or ""
    normalized = pin_codes.normalize_pin(raw_pin)
    if normalized:
        cleaned["postal_code"] = normalized
    elif raw_pin:
        # Keep the text for the rejection note, but never use it as a locator.
        cleaned["postal_code"] = ""
    return cleaned


def _state_key(value):
    key = normalize(value)
    return normalize(_STATE_ALIASES.get(key, key))


def states_equal(first, second):
    return bool(first and second and _state_key(first) == _state_key(second))


def state_from_hint(company_location):
    key = normalize(company_location)
    if not key:
        return ""
    candidates = sorted(pin_codes.states(), key=len, reverse=True)
    for state in candidates:
        if normalize(state) in key:
            return state
    for alias in _STATE_ALIASES:
        if alias in key:
            canonical = _state_key(alias)
            for state in pin_codes.states():
                if _state_key(state) == canonical:
                    return state
    return ""


def city_state_map():
    """Known city -> state aliases used by page matching and context fill."""
    return dict(_CITY_STATE)


def city_from_hint(company_location):
    key = normalize(company_location)
    if not key:
        return ""
    for city in sorted(_CITY_STATE, key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(city), key):
            return city.title()
    return ""


def _window(page_text, quote="", pin=""):
    text = str(page_text or "")
    if not text:
        return ""
    anchor = -1
    quote_text = str(quote or "").strip()
    if quote_text:
        needle = quote_text[:80].lower()
        anchor = text.lower().find(needle)
    if anchor < 0 and pin:
        anchor = text.find(pin)
    if anchor < 0:
        return text[:4000]
    return text[max(0, anchor - 400):anchor + 900]


def page_hint(page_text, quote="", pin=""):
    """Best city/state mentioned near the address on the page."""
    window = _window(page_text, quote=quote, pin=pin)
    key = normalize(window)
    if not key:
        return {"city": "", "state": ""}

    state = ""
    for candidate in sorted(pin_codes.states(), key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(normalize(candidate)), key):
            state = candidate
            break

    city = ""
    for candidate in sorted(_CITY_STATE, key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(candidate), key):
            city = candidate.title()
            break
    return {"city": city, "state": state}


def fill(fields, *, company_location="", page_text="", quote="", india=True):
    """Return saveable fields plus provenance and conflict notes.

    Result keys: ``fields``, ``pin``, ``derived``, ``conflicts``,
    ``page_hint``. ``fields`` always has the six address columns.
    """
    cleaned = clean_fields(fields, india=india)
    derived, conflicts = [], []
    raw_pin = str((fields or {}).get("postal_code") or "").strip()
    pin = (pin_codes.lookup(cleaned.get("postal_code")) if india else
           {"pin": raw_pin, "valid": False, "state": "", "district": "",
            "city": "", "lat": None, "lon": None})
    if india and raw_pin and not pin["valid"]:
        conflicts.append("invalid_pin:%s" % raw_pin)
        cleaned["postal_code"] = ""

    hint_state = state_from_hint(company_location)
    hint_city = city_from_hint(company_location)
    page = page_hint(page_text, quote=quote, pin=pin["pin"])

    # Country: a valid Indian PIN is authoritative; otherwise use the card's
    # country hint, then default to India for this workbook.
    if pin["valid"]:
        if not cleaned["country"]:
            cleaned["country"] = "India"
            derived.append("country=India from pin %s" % pin["pin"])
    elif not cleaned["country"]:
        code = geo.country_filter_for(company_location)
        name = geo.country_name_for(code)
        if name:
            cleaned["country"] = name
            derived.append("country=%s from companyLocation" % name)

    # State: explicit text wins, then PIN, then the page next to this address,
    # then the card hint.
    if not cleaned["state"]:
        for value, source in ((pin["state"], "pin"), (page["state"], "page"),
                              (hint_state, "companyLocation")):
            if value:
                cleaned["state"] = value
                derived.append("state=%s from %s" % (value, source))
                break
    elif pin["valid"] and pin["state"] and not states_equal(cleaned["state"],
                                                            pin["state"]):
        # A valid PIN is stronger evidence for the state than a model-written
        # state that may have copied the contact's own location. Correct it
        # and keep a breadcrumb instead of discarding an otherwise good street
        # address.
        corrected = cleaned["state"]
        cleaned["state"] = pin["state"]
        derived.append("state=%s from pin %s (was %s)"
                       % (pin["state"], pin["pin"], corrected))
        conflicts.append("state_pin_corrected:%s->%s"
                         % (corrected, pin["state"]))

    # City: explicit text wins, then the page next to this address, then the
    # card hint, then the PIN's city/district as a last resort. Page before
    # card hint matters for multi-office pages: a Whitefield branch must not
    # inherit the contact's Hyderabad city just because the card says so.
    if not cleaned["city"]:
        for value, source in ((page["city"], "page"),
                              (hint_city, "companyLocation"),
                              (pin["city"], "pin"), (pin["district"], "pin")):
            if value:
                cleaned["city"] = value
                derived.append("city=%s from %s" % (value, source))
                break
    elif page["city"] and normalize(page["city"]) != normalize(cleaned["city"]):
        # Do not overwrite; just leave a breadcrumb for the attempt log.
        derived.append("page_city_disagrees:%s" % page["city"])

    return {
        "fields": cleaned,
        "pin": pin,
        "derived": derived,
        "conflicts": conflicts,
        "page_hint": page,
    }


def deliverable(fields, *, india=True):
    """Street line plus either a valid PIN or a city. Nothing else is required."""
    street = normalize((fields or {}).get("address_line1"))
    if not street:
        return False
    if not india:
        return bool(normalize((fields or {}).get("city")))
    pin = pin_codes.lookup((fields or {}).get("postal_code"))
    return bool(pin["valid"] or normalize((fields or {}).get("city")))
