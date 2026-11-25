"""
Europe PMC data extraction.

Uses only the official Europe PMC REST API (no HTML scraping):
  https://europepmc.org/RestfulWebService

Two sources of email addresses per article:
  1. The `affiliation` fields returned by the search API (resultType=core).
     PubMed-derived records often carry "Electronic address: x@y.edu".
  2. The JATS full-text XML for open-access articles, where the <contrib>
     and <author-notes> blocks hold correspondence addresses.
"""

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from countries import detect_country

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
USER_AGENT = "epmc-affiliation-extractor/1.0 (local research tool; python-urllib)"

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}"
)

GENERIC_LOCAL_PARTS = {
    "info", "support", "permissions", "permission", "journals", "journal",
    "editor", "editors", "editorial", "editorialoffice", "office", "contact",
    "admin", "webmaster", "help", "sales", "noreply", "no-reply", "privacy",
    "copyright", "reprints", "subscriptions", "customerservice", "press",
}

PUBLISHER_DOMAINS = {
    "elsevier.com", "wiley.com", "springer.com", "springernature.com",
    "nature.com", "tandf.co.uk", "taylorandfrancis.com", "sagepub.com",
    "bmj.com", "plos.org", "frontiersin.org", "mdpi.com", "hindawi.com",
    "dovepress.com", "karger.com", "thelancet.com", "oup.com",
    "cambridge.org", "copyright.com", "creativecommons.org", "example.com",
    "ncbi.nlm.nih.gov", "biomedcentral.com", "wolterskluwer.com",
}


class Throttle:
    """Keeps requests polite: at most one every `interval` seconds."""

    def __init__(self, interval=0.15):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            gap = time.time() - self._last
            if gap < self.interval:
                time.sleep(self.interval - gap)
            self._last = time.time()


THROTTLE = Throttle()


def _log(msg):
    # Printed to the console the server is running in, so network activity
    # (or the lack of it) is visible instead of silently stalling the UI.
    print("[epmc] %s" % msg, file=sys.stderr, flush=True)


def http_get(url, params=None, timeout=30, retries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(retries):
        THROTTLE.wait()
        _log("GET %s  (attempt %d/%d)" % (url, attempt + 1, retries))
        start = time.time()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                _log("  -> HTTP %s in %.1fs, %d bytes" %
                     (response.status, time.time() - start, len(body)))
                return body
        except urllib.error.HTTPError as exc:
            _log("  -> HTTPError %s after %.1fs" % (exc, time.time() - start))
            if exc.code in (404, 400):
                raise
            last_error = exc
        except Exception as exc:  # timeouts, DNS, resets
            _log("  -> %r after %.1fs" % (exc, time.time() - start))
            last_error = exc
        time.sleep(1.5 * (attempt + 1))
    _log("GET %s FAILED after %d attempts: %r" % (url, retries, last_error))
    # pyrefly: ignore [bad-raise]
    raise last_error


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def clean_emails(text, drop_generic=True):
    if not text:
        return []
    text = text.replace("(at)", "@").replace("[at]", "@").replace(" at ", " at ")
    found = []
    for raw in EMAIL_RE.findall(text):
        email = raw.strip(" .,;:)]}>'\"").lower()
        if email.endswith("."):
            email = email[:-1]
        for tld in [".edu", ".org", ".gov", ".com", ".net", ".ch", ".de", ".uk", ".fr", ".it", ".es", ".nl", ".se", ".no", ".dk", ".fi", ".be", ".at", ".au", ".ca", ".jp", ".kr", ".cn", ".in", ".br", ".il", ".nz", ".sg", ".pl", ".cz", ".ie", ".gr", ".pt", ".za", ".cl"]:
            for suffix in ["no", "tel", "fax", "phone"]:
                if email.endswith(tld + suffix):
                    email = email[:-len(suffix)]
                    break
        if "@" not in email:
            continue
        local, _, domain = email.partition("@")
        if drop_generic:
            if local in GENERIC_LOCAL_PARTS:
                continue
            if any(domain == d or domain.endswith("." + d) for d in PUBLISHER_DOMAINS):
                continue
        if email not in found:
            found.append(email)
    return found


def match_email_to_name(email, author_names):
    local_part = email.split('@')[0].lower()
    local_part_letters = re.sub(r'[^a-z]', '', local_part)
    
    if not local_part_letters:
        return ""
        
    best_match = ""
    best_score = 0
    
    for name in author_names:
        if not name: continue
        parts = [p.lower() for p in re.findall(r'[a-zA-Z]+', name)]
        if not parts: continue
        
        score = 0
        for part in parts:
            if len(part) >= 2 and part in local_part_letters:
                score += len(part) * 2
            elif len(part) == 1 and part in local_part_letters:
                score += 1
                
        initials = "".join(p[0] for p in parts if p)
        if len(initials) >= 2 and initials in local_part_letters:
            score += 3
            
        if score > best_score:
            best_score = score
            best_match = name
            
    return best_match if best_score >= 4 else ""



def strip_namespaces(root):
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def element_text(element):
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def parse_fulltext(xml_bytes, drop_generic=True):
    """
    Return a list of {name, email, affiliation, corresponding} from JATS XML.
    Emails that cannot be tied to a named author are still returned, with an
    empty name, so nothing is silently dropped.
    """
    try:
        root = strip_namespaces(ET.fromstring(xml_bytes))
    except ET.ParseError:
        return []

    front = root.find("front")
    scope = front if front is not None else root

    # id -> affiliation text
    affiliations = {}
    for aff in scope.iter("aff"):
        text = element_text(aff)
        text = re.sub(r"^\s*\d+\s*", "", text)
        if aff.get("id"):
            affiliations[aff.get("id")] = text

    # id -> emails, for <corresp>, <fn>, <aff> and anything else carrying an id
    email_by_id = {}
    for element in scope.iter():
        eid = element.get("id")
        if not eid:
            continue
        emails = clean_emails(element_text(element), drop_generic)
        if emails:
            email_by_id[eid] = emails

    people = []
    assigned = set()

    for contrib in scope.iter("contrib"):
        ctype = contrib.get("contrib-type")
        if ctype not in (None, "author"):
            continue

        name_el = contrib.find("name")
        if name_el is not None:
            surname = (name_el.findtext("surname") or "").strip()
            given = (name_el.findtext("given-names") or "").strip()
            name = " ".join(p for p in (given, surname) if p)
        else:
            name = (contrib.findtext("string-name") or contrib.findtext("collab") or "").strip()
            name = re.sub(r"\s+", " ", name)

        rids_aff, rids_other = [], []
        for xref in contrib.iter("xref"):
            rid = xref.get("rid") or ""
            for part in rid.split():
                if xref.get("ref-type") == "aff":
                    rids_aff.append(part)
                else:
                    rids_other.append(part)

        affiliation = ""
        for rid in rids_aff:
            if rid in affiliations:
                affiliation = affiliations[rid]
                break
        if not affiliation:
            inline = contrib.find("aff")
            if inline is not None:
                affiliation = element_text(inline)
        if not affiliation and len(affiliations) == 1:
            affiliation = next(iter(affiliations.values()))

        emails = []
        for email_el in contrib.iter("email"):
            emails.extend(clean_emails(element_text(email_el), drop_generic))
        is_corresponding = contrib.get("corresp") == "yes" or bool(emails)
        if not emails:
            for rid in rids_other:
                if rid in email_by_id:
                    emails.extend(email_by_id[rid])
                    is_corresponding = True

        for email in dict.fromkeys(emails):
            assigned.add(email)
            people.append({
                "name": name,
                "email": email,
                "affiliation": affiliation,
                "corresponding": is_corresponding,
            })

    # Correspondence emails never linked to a contrib element.
    leftovers = []
    for notes in scope.iter("author-notes"):
        leftovers.extend(clean_emails(element_text(notes), drop_generic))
    for emails in email_by_id.values():
        leftovers.extend(emails)

    named_corresponding = [p for p in people if p["corresponding"]]
    for email in dict.fromkeys(leftovers):
        if email in assigned:
            continue
        assigned.add(email)
        people.append({
            "name": "",
            "email": email,
            "affiliation": affiliations.get(next(iter(affiliations), ""), "") if affiliations else "",
            "corresponding": True,
        })

    return people


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def build_query(keyword, year_from, year_to, open_access_only=False,
                english_only=False, extra="", selected_countries=None):
    parts = []
    keyword = (keyword or "").strip()
    if keyword:
        parts.append("(%s)" % keyword)
    if year_from and year_to:
        parts.append("(PUB_YEAR:[%d TO %d])" % (int(year_from), int(year_to)))
    if open_access_only:
        parts.append("(OPEN_ACCESS:Y)")
    if english_only:
        parts.append('(LANG:"eng")')
    if selected_countries:
        country_queries = ['AFF:"%s"' % c for c in selected_countries]
        parts.append("(" + " OR ".join(country_queries) + ")")
    if extra.strip():
        parts.append("(%s)" % extra.strip())
    return " AND ".join(parts)


def search(query, limit, page_size=500, on_page=None, should_stop=None,
           sort="P_PDATE_D desc"):
    """
    Yield core result dicts, following cursorMark pagination.

    A `sort` is passed explicitly (rather than leaving Europe PMC to use its
    default relevance ranking) because relevance order is not guaranteed
    stable across cursorMark pages for large result sets: without a stable
    sort, Europe PMC can start handing back an empty page, or the same
    cursor twice, well before the requested limit or the total hit count is
    reached. That looks identical to "finished normally" unless we flag it,
    so we log it and pass the reason back via on_page("stopped_early", ...).
    """
    cursor = "*"
    fetched = 0
    hit_count = None
    page_num = 0
    while fetched < limit:
        if should_stop and should_stop():
            return
        page_num += 1
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": min(page_size, limit - fetched),
            "cursorMark": cursor,
        }
        if sort:
            params["sort"] = sort
        payload = json.loads(http_get(BASE + "/search", params).decode("utf-8", "replace"))
        if hit_count is None:
            hit_count = payload.get("hitCount", 0)
            if on_page:
                on_page("hits", hit_count)
        results = payload.get("resultList", {}).get("result", [])
        _log("search page %d: %d results (fetched %d/%d requested, %s matching total)" %
             (page_num, len(results), fetched, limit, hit_count))

        if not results:
            if fetched < limit and hit_count and fetched < hit_count:
                reason = ("Europe PMC returned an empty page after %d of %d "
                          "requested articles (%d matched in total). Pagination "
                          "stopped early." % (fetched, limit, hit_count))
                _log("  " + reason)
                if on_page:
                    on_page("stopped_early", reason)
            return

        for result in results:
            yield result
            fetched += 1
            if fetched >= limit:
                return

        next_cursor = payload.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor:
            if fetched < limit and hit_count and fetched < hit_count:
                reason = ("Europe PMC stopped issuing new page cursors after %d of "
                          "%d requested articles (%d matched in total). Pagination "
                          "stopped early." % (fetched, limit, hit_count))
                _log("  " + reason)
                if on_page:
                    on_page("stopped_early", reason)
            return
        cursor = next_cursor


def check_connectivity(timeout=8):
    """
    Quick reachability check against Europe PMC, meant to be run once at
    startup so a blocked/firewalled connection shows up immediately instead
    of only surfacing after a job has been sitting at 0 for a while.
    Returns (ok, message).
    """
    url = BASE + "/search?query=test&format=json&pageSize=1"
    try:
        start = time.time()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(200)
            return True, "reachable (%.1fs, HTTP %s)" % (time.time() - start, response.status)
    except Exception as exc:
        return False, "%r" % exc


def fetch_fulltext(result):
    pmcid = result.get("pmcid")
    if not pmcid:
        return None
    try:
        return http_get("%s/%s/fullTextXML" % (BASE, pmcid), timeout=45, retries=2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

COLUMNS = [
    "title", "author_name", "email", "country", "affiliation",
    "is_corresponding", "journal", "pub_year", "doi", "pmid", "pmcid",
    "article_url", "country_source",
]


def article_url(result):
    if result.get("doi"):
        return "https://doi.org/" + result["doi"]
    source, ident = result.get("source"), result.get("id")
    if source and ident:
        return "https://europepmc.org/article/%s/%s" % (source, ident)
    return ""


def author_index(result):
    """name (lowercased) -> affiliation text, from the search API."""
    index = {}
    for author in result.get("authorList", {}).get("author", []):
        affiliations = [author.get("affiliation") or ""]
        details = author.get("authorAffiliationDetailsList", {}).get("authorAffiliation", [])
        affiliations += [d.get("affiliation", "") for d in details]
        text = " ".join(a for a in affiliations if a).strip()
        for key in filter(None, [author.get("fullName"), author.get("authorString")]):
            index[key.lower()] = text
    return index


def collect_people(result, fulltext_xml, drop_generic=True):
    """Merge email candidates from the search record and the full text."""
    people = []

    all_authors = []
    for author in result.get("authorList", {}).get("author", []):
        name = author.get("fullName") or ""
        if name:
            all_authors.append(name)
            
        blocks = [author.get("affiliation") or ""]
        details = author.get("authorAffiliationDetailsList", {}).get("authorAffiliation", [])
        blocks += [d.get("affiliation", "") for d in details]
        affiliation = " ".join(b for b in blocks if b).strip()
        for email in clean_emails(affiliation, drop_generic):
            people.append({
                "name": name,
                "email": email,
                "affiliation": affiliation,
                "corresponding": "electronic address" in affiliation.lower(),
            })

    if fulltext_xml:
        people.extend(parse_fulltext(fulltext_xml, drop_generic))

    # Deduplicate by email, preferring the entry with the most information.
    best = {}
    for person in people:
        key = person["email"]
        score = (bool(person["name"]), len(person["affiliation"]))
        if key not in best or score > best[key][0]:
            best[key] = (score, person)
            
    final_people = [entry[1] for entry in best.values()]
    for person in final_people:
        if not person["name"]:
            matched_name = match_email_to_name(person["email"], all_authors)
            if matched_name:
                person["name"] = matched_name
                
    return final_people


def fallback_affiliations(result):
    texts = [result.get("affiliation") or ""]
    for author in result.get("authorList", {}).get("author", []):
        if author.get("affiliation"):
            texts.append(author["affiliation"])
    return [t for t in texts if t]


def build_rows(result, fulltext_xml, settings):
    drop_generic = settings.get("drop_generic", True)
    keep_high_only = settings.get("high_income_only", True)
    keep_unknown = settings.get("keep_unknown_country", False)
    selected_countries = set(settings.get("selected_countries") or [])

    people = collect_people(result, fulltext_xml, drop_generic)
    if not people:
        return []

    index = author_index(result)
    spare = fallback_affiliations(result)
    rows = []

    for person in people:
        affiliation = person["affiliation"]
        if not affiliation and person["name"]:
            affiliation = index.get(person["name"].lower(), "")

        country, group = detect_country(affiliation)
        source = "author" if country else ""

        if not country:
            for text in spare:
                country, group = detect_country(text)
                if country:
                    affiliation = affiliation or text
                    source = "article"
                    break

        if not country:
            source, group = "unknown", "unknown"
            if not keep_unknown:
                continue
        elif selected_countries and country not in selected_countries:
            continue
        elif not selected_countries and keep_high_only and group != "high":
            continue

        rows.append({
            "title": (result.get("title") or "").rstrip("."),
            "author_name": person["name"],
            "email": person["email"],
            "country": country,
            "affiliation": affiliation,
            "is_corresponding": "yes" if person["corresponding"] else "",
            "journal": result.get("journalInfo", {}).get("journal", {}).get("title", ""),
            "pub_year": result.get("pubYear", ""),
            "doi": result.get("doi", ""),
            "pmid": result.get("pmid", ""),
            "pmcid": result.get("pmcid", ""),
            "article_url": article_url(result),
            "country_source": source,
        })
    return rows


def run_extraction(settings, progress, should_stop):
    """
    settings: keyword, year_from, year_to, limit, open_access_only,
              high_income_only, keep_unknown_country, drop_generic,
              use_fulltext, english_only, extra_query, workers
    progress(**fields) is called as work proceeds.
    Returns (rows, stats).
    """
    query = build_query(
        settings["keyword"], settings["year_from"], settings["year_to"],
        settings.get("open_access_only", False),
        settings.get("english_only", False),
        settings.get("extra_query", ""),
        settings.get("selected_countries", []),
    )
    progress(query=query, stage="searching")

    stats = {"articles": 0, "with_email": 0, "excluded_country": 0, "unknown_country": 0}
    rows, seen = [], set()
    workers = max(1, int(settings.get("workers", 5)))
    use_fulltext = settings.get("use_fulltext", True)

    def on_page(kind, value):
        if kind == "hits":
            progress(hit_count=value)
        elif kind == "stopped_early":
            progress(stopped_early=value)

    batch = []
    pool = ThreadPoolExecutor(max_workers=workers)

    def flush(batch):
        if use_fulltext:
            texts = list(pool.map(fetch_fulltext, batch))
        else:
            texts = [None] * len(batch)
        for result, xml in zip(batch, texts):
            stats["articles"] += 1
            new_rows = build_rows(result, xml, settings)
            if new_rows:
                stats["with_email"] += 1
            for row in new_rows:
                key = row["email"].lower()
                if key in seen:
                    continue
                seen.add(key)
                if row["country_source"] == "unknown":
                    stats["unknown_country"] += 1
                rows.append(row)
        progress(processed=stats["articles"], rows=len(rows), stage="fetching")

    try:
        for result in search(query, settings["limit"], on_page=on_page,
                             should_stop=should_stop):
            batch.append(result)
            if len(batch) >= 25:
                flush(batch)
                batch = []
            if should_stop():
                break
        if batch and not should_stop():
            flush(batch)
    finally:
        pool.shutdown(wait=False)

    progress(stage="done", processed=stats["articles"], rows=len(rows))
    return rows, stats