"""
PubMed data extraction.

Uses NCBI E-utilities API.
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
from epmc_extractor import parse_fulltext

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
USER_AGENT = "pubmed-affiliation-extractor/1.0 (local research tool)"

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
    def __init__(self, interval=0.34): # max 3 per second without API key
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
    print("[pubmed] %s" % msg, file=sys.stderr, flush=True)

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
        except Exception as exc:
            _log("  -> %r after %.1fs" % (exc, time.time() - start))
            last_error = exc
        time.sleep(1.5 * (attempt + 1))
    _log("GET %s FAILED after %d attempts: %r" % (url, retries, last_error))
    if last_error is not None:
        raise last_error
    raise RuntimeError("HTTP request failed")


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


def build_query(keyword, year_from, year_to, open_access_only=False, english_only=False, extra="", selected_countries=None):
    parts = []
    keyword = (keyword or "").strip()
    if keyword:
        parts.append("(%s)" % keyword)
    if year_from and year_to:
        parts.append('("%d/01/01"[Date - Publication] : "%d/12/31"[Date - Publication])' % (int(year_from), int(year_to)))
    if open_access_only:
        parts.append('("free full text"[Filter] OR "pmc"[Filter])')
    if english_only:
        parts.append('("english"[Language])')
    if selected_countries:
        country_queries = ['"%s"[Affiliation]' % c for c in selected_countries]
        parts.append("(" + " OR ".join(country_queries) + ")")
    if extra.strip():
        parts.append("(%s)" % extra.strip())
    return " AND ".join(parts)


def search(query, limit, page_size=500, on_page=None, should_stop=None):
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(min(limit, 100000)),
        "usehistory": "y"
    }
    
    try:
        response_raw = http_get(BASE + "/esearch.fcgi", params)
        payload = json.loads(response_raw)
        
        hit_count = int(payload.get("esearchresult", {}).get("count", 0))
        if on_page:
            on_page("hits", hit_count)
            
        webenv = payload.get("esearchresult", {}).get("webenv")
        query_key = payload.get("esearchresult", {}).get("querykey")
        
        fetched = 0
        while fetched < min(limit, hit_count):
            if should_stop and should_stop():
                return
                
            fetch_count = min(page_size, limit - fetched, hit_count - fetched)
            
            fetch_params = {
                "db": "pubmed",
                "retmode": "xml",
                "retstart": str(fetched),
                "retmax": str(fetch_count),
                "query_key": query_key,
                "WebEnv": webenv
            }
            
            xml_raw = http_get(BASE + "/efetch.fcgi", fetch_params)
            
            try:
                root = ET.fromstring(xml_raw)
                articles = root.findall(".//PubmedArticle")
                for article in articles:
                    yield article
            except ET.ParseError:
                _log("Failed to parse PubMed XML")
            
            fetched += fetch_count

    except Exception as e:
        _log("Search failed: %s" % e)


def check_connectivity(timeout=8):
    url = BASE + "/esearch.fcgi?db=pubmed&term=test&retmode=json&retmax=1"
    try:
        start = time.time()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(200)
            return True, "reachable (%.1fs, HTTP %s)" % (time.time() - start, response.status)
    except Exception as exc:
        return False, "%r" % exc


COLUMNS = [
    "title", "author_name", "email", "country", "affiliation",
    "is_corresponding", "journal", "pub_year", "doi", "pmid", "pmcid",
    "article_url", "country_source",
]

def get_article_pmcid(article):
    for id_el in article.findall(".//ArticleId"):
        if id_el.get("IdType") == "pmc":
            val = (id_el.text or "").strip()
            if val:
                if not val.startswith("PMC") and val.isdigit():
                    val = "PMC" + val
                return val
    return None


def fetch_pmc_fulltext(pmcid):
    if not pmcid:
        return None
    try:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/%s/fullTextXML" % pmcid
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def element_text(el):
    if el is None:
        return ""
    return "".join(el.itertext()).strip()

def extract_article_data(article, settings, fulltext_xml=None):
    medline_cit = article.find("MedlineCitation")
    if medline_cit is None:
        return []
        
    pmid_el = medline_cit.find("PMID")
    pmid = pmid_el.text if pmid_el is not None else ""
    
    article_el = medline_cit.find("Article")
    if article_el is None:
        return []
        
    title = element_text(article_el.find("ArticleTitle"))
    journal = element_text(article_el.find(".//Title"))
    pub_year = element_text(article_el.find(".//PubDate/Year"))
    if not pub_year:
        medline_date = element_text(article_el.find(".//PubDate/MedlineDate"))
        if medline_date:
            m = re.search(r'\d{4}', medline_date)
            if m: pub_year = m.group(0)
    
    doi = ""
    pmcid = ""
    for id_el in article.findall(".//ArticleId"):
        id_type = id_el.get("IdType")
        if id_type == "doi": doi = id_el.text
        elif id_type == "pmc": pmcid = id_el.text
        
    people = []
    all_authors = []
    
    for author in article_el.findall(".//Author"):
        last_name = element_text(author.find("LastName"))
        fore_name = element_text(author.find("ForeName"))
        name = f"{fore_name} {last_name}".strip()
        if name:
            all_authors.append(name)
            
        affiliations = [element_text(aff) for aff in author.findall(".//Affiliation")]
        affiliation = " ".join(affiliations).strip()
        
        for email in clean_emails(affiliation, settings.get("drop_generic", True)):
            people.append({
                "name": name,
                "email": email,
                "affiliation": affiliation,
                "corresponding": "Electronic address:" in affiliation or "corresponding" in affiliation.lower(),
            })

    # Capture article-level affiliations if any
    for aff in article_el.findall(".//AffiliationInfo/Affiliation"):
        aff_text = element_text(aff)
        for email in clean_emails(aff_text, settings.get("drop_generic", True)):
            people.append({
                "name": "",
                "email": email,
                "affiliation": aff_text,
                "corresponding": True,
            })

    # If full text XML from PMC is available, parse all correspondence authors and emails
    if fulltext_xml:
        people.extend(parse_fulltext(fulltext_xml, settings.get("drop_generic", True)))

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
                
    drop_generic = settings.get("drop_generic", True)
    keep_high_only = settings.get("high_income_only", True)
    keep_unknown = settings.get("keep_unknown_country", False)
    selected_countries = set(settings.get("selected_countries") or [])
    
    rows = []
    for person in final_people:
        affiliation = person["affiliation"]
        country, group = detect_country(affiliation)
        source = "author" if country else ""
        
        if not country:
            source, group = "unknown", "unknown"
            if not keep_unknown:
                continue
        elif selected_countries and country not in selected_countries:
            continue
        elif not selected_countries and keep_high_only and group != "high":
            continue
            
        rows.append({
            "title": title,
            "author_name": person["name"],
            "email": person["email"],
            "country": country,
            "affiliation": affiliation,
            "is_corresponding": "yes" if person["corresponding"] else "",
            "journal": journal,
            "pub_year": pub_year,
            "doi": doi,
            "pmid": pmid,
            "pmcid": pmcid,
            "article_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            "country_source": source,
        })
        
    return rows

def run_extraction(settings, progress, should_stop):
    query = build_query(
        settings["keyword"], settings["year_from"], settings["year_to"],
        settings.get("open_access_only", False),
        settings.get("english_only", False),
        settings.get("extra_query", ""),
        settings.get("selected_countries", [])
    )
    progress(query=query, stage="searching")

    stats = {"articles": 0, "with_email": 0, "excluded_country": 0, "unknown_country": 0, "skipped_existing": 0}
    rows, seen = [], set()
    exclude_emails = settings.get("exclude_emails") or set()

    def on_page(kind, value):
        if kind == "hits":
            progress(hit_count=value)
        elif kind == "stopped_early":
            progress(stopped_early=value)

    workers = max(1, int(settings.get("workers", 8)))
    pool = ThreadPoolExecutor(max_workers=workers)
    batch = []

    def flush(batch):
        pmcids = [get_article_pmcid(a) for a in batch]
        if any(pmcids):
            texts = list(pool.map(fetch_pmc_fulltext, pmcids))
        else:
            texts = [None] * len(batch)

        for article, xml in zip(batch, texts):
            stats["articles"] += 1
            new_rows = extract_article_data(article, settings, xml)
            if new_rows:
                stats["with_email"] += 1
            for row in new_rows:
                key = row["email"].lower()
                if key in seen:
                    continue
                if key in exclude_emails:
                    stats["skipped_existing"] += 1
                    continue
                seen.add(key)
                if row["country_source"] == "unknown":
                    stats["unknown_country"] += 1
                rows.append(row)
        progress(processed=stats["articles"], rows=len(rows), stage="fetching")

    try:
        for article in search(query, settings["limit"], on_page=on_page, should_stop=should_stop):
            batch.append(article)
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
