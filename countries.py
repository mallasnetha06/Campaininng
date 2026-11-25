"""
Country detection and income-group classification for author affiliation strings.

The high-income list follows the World Bank classification (FY2025).
Edit HIGH_INCOME below if your project needs a different definition
(e.g. OECD members only, or "Global North").
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# World Bank high-income economies. Key = canonical name, value = extra aliases
# as they actually appear in affiliation strings.
# ---------------------------------------------------------------------------

HIGH_INCOME = {
    "Andorra": [],
    "Antigua and Barbuda": ["antigua"],
    "Aruba": [],
    "Australia": ["australien"],
    "Austria": ["osterreich", "oesterreich"],
    "Bahamas": ["the bahamas"],
    "Bahrain": [],
    "Barbados": [],
    "Belgium": ["belgique", "belgie", "belgien"],
    "Bermuda": [],
    "British Virgin Islands": [],
    "Brunei": ["brunei darussalam"],
    "Bulgaria": ["bulgaria"],
    "Canada": ["quebec canada"],
    "Cayman Islands": [],
    "Chile": [],
    "Croatia": ["hrvatska"],
    "Curacao": ["curaçao"],
    "Cyprus": [],
    "Czechia": ["czech republic", "ceska republika"],
    "Denmark": ["danmark"],
    "Estonia": ["eesti"],
    "Faroe Islands": ["faroe islands"],
    "Finland": ["suomi"],
    "France": ["francia"],
    "French Polynesia": [],
    "Germany": ["deutschland", "allemagne", "germay"],
    "Gibraltar": [],
    "Greece": ["hellas", "ellas"],
    "Greenland": [],
    "Guam": [],
    "Guyana": [],
    "Hong Kong": ["hong kong sar", "hongkong", "hong kong s.a.r"],
    "Hungary": ["magyarorszag"],
    "Iceland": ["island iceland"],
    "Ireland": ["eire", "republic of ireland"],
    "Isle of Man": [],
    "Israel": ["isreal"],
    "Italy": ["italia", "italie", "itay"],
    "Japan": ["nippon", "japan."],
    "Kuwait": [],
    "Latvia": ["latvija"],
    "Liechtenstein": [],
    "Lithuania": ["lietuva"],
    "Luxembourg": [],
    "Macao": ["macau", "macao sar"],
    "Malta": [],
    "Monaco": [],
    "Nauru": [],
    "Netherlands": ["the netherlands", "nederland", "holland", "netherland"],
    "New Caledonia": [],
    "New Zealand": ["aotearoa"],
    "Northern Mariana Islands": [],
    "Norway": ["norge"],
    "Oman": ["sultanate of oman"],
    "Palau": [],
    "Panama": [],
    "Poland": ["polska"],
    "Portugal": [],
    "Puerto Rico": [],
    "Qatar": [],
    "Romania": ["romania"],
    "Russia": ["russian federation", "russia."],
    "San Marino": [],
    "Saudi Arabia": ["kingdom of saudi arabia", "ksa"],
    "Seychelles": [],
    "Singapore": ["republic of singapore"],
    "Sint Maarten": [],
    "Slovakia": ["slovak republic", "slovensko"],
    "Slovenia": ["slovenija"],
    "South Korea": [
        "korea", "republic of korea", "korea republic", "rep of korea",
        "south korea", "korea rep", "r o korea", "republic of korea rok",
    ],
    "Spain": ["espana", "españa", "espagne"],
    "St Kitts and Nevis": ["saint kitts and nevis", "st kitts"],
    "St Martin": ["saint martin"],
    "Sweden": ["sverige"],
    "Switzerland": ["suisse", "schweiz", "svizzera", "switerland"],
    "Taiwan": ["taiwan roc", "republic of china taiwan", "chinese taipei", "taiwan r o c"],
    "Trinidad and Tobago": ["trinidad"],
    "Turks and Caicos Islands": [],
    "United Arab Emirates": ["uae", "u a e", "abu dhabi uae", "emirates"],
    "United Kingdom": [
        "uk", "u k", "great britain", "england", "scotland", "wales",
        "northern ireland", "britain", "united kingdom of great britain",
    ],
    "United States": [
        "usa", "u s a", "united states of america", "us a", "the usa",
        "united states", "u s", "america",
    ],
    "Uruguay": [],
    "US Virgin Islands": ["u s virgin islands", "virgin islands"],
}

# Common non-high-income countries, so we can say "detected but excluded"
# rather than "unknown". Not exhaustive; unknowns are reported separately.
OTHER_INCOME = {
    "China": ["prc", "p r china", "peoples republic of china", "pr china", "china."],
    "India": ["bharat"],
    "Brazil": ["brasil"],
    "Mexico": ["mexico"],
    "Turkiye": ["turkey", "turkiye", "türkiye"],
    "Iran": ["islamic republic of iran", "i r iran"],
    "Egypt": ["arab republic of egypt"],
    "Nigeria": [],
    "South Africa": [],
    "Pakistan": [],
    "Bangladesh": [],
    "Indonesia": [],
    "Thailand": [],
    "Vietnam": ["viet nam"],
    "Malaysia": [],
    "Philippines": ["the philippines"],
    "Colombia": [],
    "Peru": [],
    "Argentina": [],
    "Ukraine": [],
    "Serbia": [],
    "Bosnia and Herzegovina": ["bosnia"],
    "North Macedonia": ["macedonia"],
    "Montenegro": [],
    "Albania": [],
    "Kosovo": [],
    "Georgia": ["republic of georgia"],
    "Armenia": [],
    "Azerbaijan": [],
    "Kazakhstan": [],
    "Uzbekistan": [],
    "Kyrgyzstan": [],
    "Tajikistan": [],
    "Turkmenistan": [],
    "Mongolia": [],
    "Belarus": [],
    "Moldova": [],
    "Morocco": ["maroc"],
    "Tunisia": ["tunisie"],
    "Algeria": ["algerie"],
    "Libya": [],
    "Sudan": [],
    "Kenya": [],
    "Ethiopia": [],
    "Ghana": [],
    "Tanzania": [],
    "Uganda": [],
    "Rwanda": [],
    "Zambia": [],
    "Zimbabwe": [],
    "Malawi": [],
    "Mozambique": [],
    "Botswana": [],
    "Namibia": [],
    "Senegal": [],
    "Cameroon": [],
    "Ivory Coast": ["cote d ivoire", "côte d'ivoire"],
    "Iraq": [],
    "Jordan": ["hashemite kingdom of jordan"],
    "Lebanon": [],
    "Syria": ["syrian arab republic"],
    "Yemen": [],
    "Palestine": ["west bank", "gaza"],
    "Sri Lanka": [],
    "Nepal": [],
    "Myanmar": ["burma"],
    "Cambodia": [],
    "Laos": ["lao pdr"],
    "Afghanistan": [],
    "Bhutan": [],
    "Maldives": [],
    "Mauritius": [],
    "Fiji": [],
    "Papua New Guinea": [],
    "Cuba": [],
    "Jamaica": [],
    "Dominican Republic": [],
    "Costa Rica": [],
    "Ecuador": [],
    "Bolivia": [],
    "Paraguay": [],
    "Venezuela": [],
    "Guatemala": [],
    "Honduras": [],
    "El Salvador": [],
    "Nicaragua": [],
    "North Korea": ["democratic peoples republic of korea", "dprk"],
}

COUNTRY_NAMES = sorted(set(HIGH_INCOME) | set(OTHER_INCOME))

# Checked before anything else, in this order. Resolves "Hong Kong, China",
# "Taipei, Taiwan, ROC", "San Juan, Puerto Rico, USA".
PRIORITY = [
    ("North Korea", ["democratic people", "dprk", "north korea"]),
    ("Hong Kong", ["hong kong", "hongkong"]),
    ("Macao", ["macao", "macau"]),
    ("Taiwan", ["taiwan", "chinese taipei"]),
    ("Puerto Rico", ["puerto rico"]),
    ("South Korea", ["south korea", "republic of korea"]),
]

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
US_STATE_ABBR = set(US_STATES.values())

CANADA_PROVINCES = [
    "ontario", "quebec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
]


def _norm(text):
    """Lowercase, strip accents and punctuation noise, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9,;\n\s'-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_index():
    index = {}
    for group, table in (("high", HIGH_INCOME), ("other", OTHER_INCOME)):
        for canonical, aliases in table.items():
            keys = {_norm(canonical)} | {_norm(a) for a in aliases}
            for key in keys:
                if key and key not in index:
                    index[key] = (canonical, group)
    return index


INDEX = _build_index()
# Longest first so "united states of america" wins over "america".
INDEX_KEYS = sorted(INDEX, key=len, reverse=True)

ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

TAIL_NOISE_RE = re.compile(
    r"(?i)\b(?:electronic\s+address|e-?mails?|correspondence(?:\s+to)?|"
    r"corresponding\s+author|tel(?:ephone)?|fax|orcid|https?://)\b"
)

# Abbreviations short enough to be risky inside words, so they are only
# matched on strict word boundaries and only as a last resort.
SHORT_CODES = {
    "usa": ("United States", "high"),
    "u s a": ("United States", "high"),
    "uk": ("United Kingdom", "high"),
    "u k": ("United Kingdom", "high"),
    "uae": ("United Arab Emirates", "high"),
    "prc": ("China", "other"),
    "p r c": ("China", "other"),
    "p r china": ("China", "other"),
    "dprk": ("North Korea", "other"),
}


def _segment_match(segment):
    """Match one comma-separated tail segment of an affiliation."""
    seg = segment.strip(" .-'")
    if not seg:
        return None

    # "Atlanta, GA 30322" / "Boston, Massachusetts 02115" -> United States
    bare = ZIP_RE.sub(" ", seg).strip()
    bare = re.sub(r"\s+", " ", bare)
    if bare.upper() in US_STATE_ABBR and ZIP_RE.search(seg):
        return ("United States", "high")
    if bare in US_STATES and ZIP_RE.search(seg):
        return ("United States", "high")
    if bare in CANADA_PROVINCES:
        return ("Canada", "high")

    if bare in INDEX:
        return INDEX[bare]

    # Country name sitting at the end of the segment, e.g. "12345 berlin germany"
    for key in INDEX_KEYS:
        if len(key) < 4:
            continue
        if re.search(r"(^|\s)" + re.escape(key) + r"$", bare):
            return INDEX[key]
    return None


def detect_country(affiliation):
    """
    Return (country_name, income_group) where income_group is
    'high', 'other' or 'unknown'. country_name is '' when unknown.
    """
    if not affiliation:
        return ("", "unknown")

    # "...Oxford, UK. Electronic address: a@b.uk" -> drop the contact clause,
    # otherwise the country stops being the last thing in the string.
    affiliation = TAIL_NOISE_RE.split(affiliation, 1)[0]

    text = _norm(affiliation)
    # Drop email addresses so "@ucl.ac.uk" doesn't confuse matching.
    text = re.sub(r"\S+@\S+", " ", text)

    for canonical, needles in PRIORITY:
        for needle in needles:
            if re.search(r"(^|\W)" + re.escape(needle) + r"(\W|$)", text):
                group = "high" if canonical in HIGH_INCOME else "other"
                return (canonical, group)

    segments = [s for s in re.split(r"[,;\n]", text) if s.strip()]
    for segment in reversed(segments[-4:]):
        hit = _segment_match(segment)
        if hit:
            return hit

    # Last resort: whole-string search, longest alias first.
    for key in INDEX_KEYS:
        if len(key) < 5:
            continue
        if re.search(r"(^|\W)" + re.escape(key) + r"(\W|$)", text):
            return INDEX[key]

    for code, value in SHORT_CODES.items():
        if re.search(r"(^|\W)" + code + r"(\W|$)", text):
            return value

    # US state + ZIP anywhere in the string.
    if ZIP_RE.search(text):
        for state in US_STATES:
            if re.search(r"(^|\W)" + state + r"(\W|$)", text):
                return ("United States", "high")

    return ("", "unknown")


def is_high_income(affiliation):
    return detect_country(affiliation)[1] == "high"