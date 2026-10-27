"""Single source of Indian city/state geography used by the address pass.

Three modules used to carry their own city and state tables
(``address_fields._CITY_STATE``/``_STATE_ALIASES``, ``geo._CITY_METRO``,
``page_quality._CITY_GST_PREFIX``/``_STATE_GST_PREFIX``). They drifted: the same
key meant different things in different tables and the Jammu & Kashmir spelling
did not match between them. Everything derives from the tables below now.
"""
import re

from . import pin_codes
from .address_text import norm

# City / suburb -> state. Suburbs are included so a page that says "Whitefield"
# still resolves to Karnataka.
CITY_STATE = {
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

# Card/page spellings -> the state name used by pin_codes.states().
STATE_ALIASES = {
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

# Suburbs that are administratively separate but the same metro for addressing.
METRO_CITY = {
    "new town": "kolkata", "newtown": "kolkata", "rajarhat": "kolkata",
    "salt lake": "kolkata", "bidhannagar": "kolkata",
    "calcutta": "kolkata",
    "madhapur": "hyderabad", "gachibowli": "hyderabad",
    "kondapur": "hyderabad", "hitech city": "hyderabad",
    "secunderabad": "hyderabad", "kukatpally": "hyderabad",
    "whitefield": "bengaluru", "electronic city": "bengaluru",
    "bangalore": "bengaluru",
    "hinjewadi": "pune", "pimpri": "pune", "pimpri chinchwad": "pune",
    "poona": "pune",
    "ahmadabad": "ahmedabad",
    "bombay": "mumbai",
    "madras": "chennai",
    "gurgaon": "gurugram",
}

# GST state code by state name.
STATE_GST_PREFIX = {
    "west bengal": "19", "telangana": "36", "maharashtra": "27",
    "karnataka": "29", "tamil nadu": "33", "delhi": "07", "gujarat": "24",
    "uttar pradesh": "09", "andhra pradesh": "37", "rajasthan": "08",
    "madhya pradesh": "23", "haryana": "06", "punjab": "03", "kerala": "32",
    "odisha": "21", "bihar": "10", "jharkhand": "20", "assam": "18",
    "chhattisgarh": "22", "goa": "30", "uttarakhand": "05",
    "himachal pradesh": "02", "jammu and kashmir": "01",
    "jammu & kashmir": "01", "chandigarh": "04",
}

_PREFIX_BY_KEY = {norm(name).replace(" and ", " "): code
                  for name, code in STATE_GST_PREFIX.items()}


def state_key(value):
    """Canonical comparison key: aliases resolved, '&'/'and' ignored."""
    key = norm(value)
    key = STATE_ALIASES.get(key, key)
    return key.replace(" and ", " ")


def _display_state(value):
    """Return the pin-table spelling for a state name, or the input."""
    key = state_key(value)
    for name in pin_codes.states():
        if state_key(name) == key:
            return name
    return str(value or "")


def states_equal(first, second):
    return bool(first and second and state_key(first) == state_key(second))


def state_from_text(text):
    """Best state named in ``text`` directly, by alias, or via a city."""
    key = norm(text)
    if not key:
        return ""
    for name in sorted(pin_codes.states(), key=len, reverse=True):
        if name and re.search(r"\b%s\b" % re.escape(norm(name)), key):
            return name
    for alias in sorted(STATE_ALIASES, key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(alias), key):
            return _display_state(STATE_ALIASES[alias])
    for city in sorted(CITY_STATE, key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(city), key):
            return _display_state(CITY_STATE[city])
    return ""


def city_from_text(text):
    """Best known city named in ``text``, title-cased, or an empty string."""
    key = norm(text)
    if not key:
        return ""
    for city in sorted(CITY_STATE, key=len, reverse=True):
        if re.search(r"\b%s\b" % re.escape(city), key):
            return city.title()
    return ""


def metro_city(value):
    """Map a suburb to its metro city for city-equality checks."""
    key = norm(value)
    return METRO_CITY.get(key, key)


def gst_prefix_for(value):
    """GST state code implied by a state name, alias, or city."""
    state = state_from_text(value) or value
    return _PREFIX_BY_KEY.get(norm(state).replace(" and ", " "), "")
