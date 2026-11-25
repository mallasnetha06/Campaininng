#!/usr/bin/env python3
"""
PubMed author-contact extractor.

Run the app:      python3 app.py            -> opens http://127.0.0.1:8765
Run headless:     python3 app.py --cli --keyword "crispr gene editing" \
                      --from 2020 --to 2024 --limit 500 --out results.csv

Standard library only. Python 3.8+.
"""

import argparse
import csv
import io
import json
import os
import re
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import socket
from concurrent.futures import ThreadPoolExecutor

from countries import COUNTRY_NAMES
import pubmed_extractor
import epmc_extractor
import mdpi_extractor

COLUMNS = pubmed_extractor.COLUMNS  # Assuming they have the same columns, otherwise we should use a superset

EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Job handling
# ---------------------------------------------------------------------------

def new_job(settings):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "settings": settings,
        "stage": "queued",
        "query": "",
        "hit_count": None,
        "processed": 0,
        "rows": 0,
        "error": "",
        "stopped_early": "",
        "stop": False,
        "started": time.time(),
        "finished": None,
        "data": [],
        "stats": {},
        "csv_path": "",
        "verifying": False,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(target=run_job, args=(job,), daemon=True)
    thread.start()
    return job_id


def run_job(job):
    def progress(**fields):
        with JOBS_LOCK:
            job.update(fields)

    def should_stop():
        return job["stop"]

    try:
        if job["settings"].get("skip_existing"):
            job["settings"]["exclude_emails"] = get_existing_emails()
            
        source = job["settings"].get("source", "pubmed")
        if source == "pubmed":
            rows, stats = pubmed_extractor.run_extraction(job["settings"], progress, should_stop)
        elif source == "epmc":
            rows, stats = epmc_extractor.run_extraction(job["settings"], progress, should_stop)
        elif source == "mdpi":
            rows, stats = mdpi_extractor.run_extraction(job["settings"], progress, should_stop)
        else:
            raise ValueError("Unknown source: %s" % source)
            
        path = write_csv_file(job, rows)
        with JOBS_LOCK:
            job["data"] = rows
            job["stats"] = stats
            job["rows"] = len(rows)
            job["csv_path"] = path
            job["stage"] = "stopped" if job["stop"] else "done"
    except Exception as exc:
        traceback.print_exc()
        with JOBS_LOCK:
            job["error"] = "%s: %s" % (type(exc).__name__, exc)
            job["stage"] = "error"
    finally:
        with JOBS_LOCK:
            job["finished"] = time.time()


def verify_emails_for_job(job):
    def check_email(row):
        email = row.get("email", "")
        if "@" not in email: return False
        domain = email.split("@")[1]
        try:
            socket.gethostbyname(domain)
            return True
        except socket.error:
            return False

    with JOBS_LOCK:
        job["verifying"] = True
        job["stage"] = "verifying"
        rows = list(job["data"])
        
    valid_rows = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(check_email, rows))
        
    seen_emails = set()
    for row, is_valid in zip(rows, results):
        if is_valid:
            e = row["email"].lower()
            if e not in seen_emails:
                seen_emails.add(e)
                valid_rows.append(row)

    path = write_csv_file(job, valid_rows)
    
    with JOBS_LOCK:
        job["data"] = valid_rows
        job["rows"] = len(valid_rows)
        job["csv_path"] = path
        job["verifying"] = False
        job["stage"] = "done"


def rows_to_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore",
                            quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def safe_slug(text, limit=40):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (text or "results")).strip("-").lower()
    return (slug[:limit] or "results")


def write_csv_file(job, rows):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    settings = job["settings"]
    source = settings.get("source", "pubmed")
    name = "%s-%s-%s-%s-%s.csv" % (
        source,
        safe_slug(settings.get("keyword")),
        settings.get("year_from"), settings.get("year_to"),
        time.strftime("%Y%m%d-%H%M%S"),
    )
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(rows_to_csv(rows))
    return path


def get_existing_emails():
    seen = set()
    if not os.path.exists(EXPORT_DIR):
        return seen
    for filename in os.listdir(EXPORT_DIR):
        if filename.endswith(".csv"):
            try:
                with open(os.path.join(EXPORT_DIR, filename), "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if "email" in row and row["email"]:
                            seen.add(row["email"].lower())
            except Exception:
                pass
    return seen


def public_state(job):
    return {
        "id": job["id"],
        "stage": job["stage"],
        "query": job["query"],
        "hit_count": job["hit_count"],
        "processed": job["processed"],
        "rows": job["rows"],
        "error": job["error"],
        "stopped_early": job.get("stopped_early", ""),
        "stats": job["stats"],
        "elapsed": round((job["finished"] or time.time()) - job["started"], 1),
        "csv_path": job["csv_path"],
        "source": job["settings"].get("source", "pubmed"),
    }


SERVER_PORT = 8765
SERVER_HOST = "0.0.0.0"
INDEX_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def format_size(bytes_num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.1f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} TB"


def list_exports():
    if not os.path.exists(EXPORT_DIR):
        return []
    exports = []
    for f in os.listdir(EXPORT_DIR):
        if f.endswith(".csv"):
            full_path = os.path.join(EXPORT_DIR, f)
            try:
                st = os.stat(full_path)
                row_count = 0
                with open(full_path, "r", encoding="utf-8-sig", errors="ignore") as fp:
                    for _ in fp:
                        row_count += 1
                row_count = max(0, row_count - 1)
                
                parts = f.replace(".csv", "").split("-")
                source = parts[0] if parts else "pubmed"
                keyword = parts[1].replace("-", " ") if len(parts) > 1 else ""

                exports.append({
                    "name": f,
                    "size": st.st_size,
                    "formatted_size": format_size(st.st_size),
                    "mtime": st.st_mtime,
                    "date": time.strftime("%b %d, %Y %I:%M %p", time.localtime(st.st_mtime)),
                    "rows": row_count,
                    "source": source,
                    "keyword": keyword,
                })
            except Exception:
                pass
    exports.sort(key=lambda x: x["mtime"], reverse=True)
    return exports


def extract_unique_emails(rows):
    seen = set()
    result = []
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if email and "@" in email and email not in seen:
            seen.add(email)
            result.append(email)
    return result


def get_page_html():
    if os.path.exists(INDEX_HTML_PATH):
        try:
            with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return PAGE


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TargetPulseStudio/2.0"

    def log_message(self, fmt, *args):
        pass  # keep console clean; job progress is shown in the UI

    def _send(self, code, body, content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload), "application/json; charset=utf-8")

    def _job(self, query):
        job_id = (parse_qs(query).get("id") or [""])[0]
        with JOBS_LOCK:
            return JOBS.get(job_id)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            return self._send(200, get_page_html(), "text/html; charset=utf-8")

        if route == "/api/status":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            return self._json(200, public_state(job))

        if route == "/api/preview":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            return self._json(200, {
                "columns": COLUMNS,
                "rows": job["data"][:500],
                "all_rows": job["data"],
                "total": len(job["data"])
            })

        if route == "/api/emails":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            emails = extract_unique_emails(job["data"])
            return self._json(200, {"emails": emails, "count": len(emails)})

        if route == "/api/csv":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            filename = os.path.basename(job["csv_path"] or "targetpulse-contacts.csv")
            body = "\ufeff" + rows_to_csv(job["data"])
            return self._send(200, body, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="%s"' % filename})

        if route == "/api/countries":
            from countries import COUNTRY_NAMES, HIGH_INCOME
            country_items = [{"name": c, "high_income": c in HIGH_INCOME} for c in COUNTRY_NAMES]
            return self._json(200, {"countries": country_items})

        if route == "/api/exports":
            return self._json(200, {"exports": list_exports()})

        if route == "/api/export_download":
            filename = (parse_qs(parsed.query).get("file") or [""])[0]
            filename = os.path.basename(filename)
            path = os.path.join(EXPORT_DIR, filename)
            if not os.path.isfile(path):
                return self._json(404, {"error": "File not found"})
            with open(path, "rb") as f:
                body = f.read()
            return self._send(200, body, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="%s"' % filename})

        if route == "/api/export_preview":
            filename = (parse_qs(parsed.query).get("file") or [""])[0]
            filename = os.path.basename(filename)
            path = os.path.join(EXPORT_DIR, filename)
            if not os.path.isfile(path):
                return self._json(404, {"error": "File not found"})
            rows = []
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append(r)
            return self._json(200, {
                "name": filename,
                "columns": COLUMNS,
                "rows": rows[:500],
                "all_rows": rows,
                "total": len(rows)
            })

        if route == "/api/system":
            return self._json(200, {
                "local_ip": get_local_ip(),
                "port": SERVER_PORT,
                "host": SERVER_HOST,
                "export_dir": EXPORT_DIR,
                "version": "2.0.0"
            })

        if route == "/api/connectivity":
            pb_ok, pb_msg = pubmed_extractor.check_connectivity()
            epmc_ok, epmc_msg = epmc_extractor.check_connectivity()
            mdpi_ok, mdpi_msg = mdpi_extractor.check_connectivity()
            return self._json(200, {
                "pubmed": {"ok": pb_ok, "message": pb_msg},
                "epmc": {"ok": epmc_ok, "message": epmc_msg},
                "mdpi": {"ok": mdpi_ok, "message": mdpi_msg}
            })

        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"error": "Body must be JSON."})

        if parsed.path == "/api/start":
            try:
                settings = normalise_settings(payload)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"id": new_job(settings)})

        if parsed.path == "/api/stop":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            job["stop"] = True
            return self._json(200, {"ok": True})
            
        if parsed.path == "/api/verify":
            job = self._job(parsed.query)
            if not job:
                return self._json(404, {"error": "unknown job"})
            threading.Thread(target=verify_emails_for_job, args=(job,), daemon=True).start()
            return self._json(200, {"ok": True})

        if parsed.path == "/api/export_delete":
            filename = os.path.basename(payload.get("file", ""))
            path = os.path.join(EXPORT_DIR, filename)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    return self._json(200, {"ok": True})
                except Exception as exc:
                    return self._json(500, {"error": str(exc)})
            return self._json(404, {"error": "File not found"})

        if parsed.path == "/api/open_folder":
            os.makedirs(EXPORT_DIR, exist_ok=True)
            if hasattr(os, "startfile"):
                try:
                    os.startfile(EXPORT_DIR)
                    return self._json(200, {"ok": True})
                except Exception as exc:
                    return self._json(500, {"error": str(exc)})
            return self._json(200, {"ok": False, "message": "Folder opening not supported on this operating system."})

        return self._send(404, "Not found", "text/plain; charset=utf-8")


def normalise_settings(payload):
    keyword = (payload.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("Enter at least one keyword.")
    try:
        year_from = int(payload.get("year_from"))
        year_to = int(payload.get("year_to"))
    except (TypeError, ValueError):
        raise ValueError("Both years must be whole numbers, e.g. 2020 and 2024.")
    if year_from > year_to:
        raise ValueError("The start year must not be later than the end year.")
    limit = max(1, min(int(payload.get("limit") or 1000), 100000))
    selected_countries = payload.get("selected_countries") or []
    if not isinstance(selected_countries, list):
      raise ValueError("Selected countries must be a list.")
    selected_countries = list(dict.fromkeys(selected_countries))
    invalid_countries = [country for country in selected_countries
               if country not in COUNTRY_NAMES]
    if invalid_countries:
      raise ValueError("Unknown country selection: %s." % invalid_countries[0])
    return {
        "keyword": keyword,
        "year_from": year_from,
        "year_to": year_to,
        "limit": limit,
        "open_access_only": bool(payload.get("open_access_only")),
        "english_only": bool(payload.get("english_only")),
        "use_fulltext": payload.get("use_fulltext", True),
        "high_income_only": payload.get("high_income_only", True),
        "keep_unknown_country": bool(payload.get("keep_unknown_country")),
        "selected_countries": selected_countries,
        "drop_generic": payload.get("drop_generic", True),
        "skip_existing": bool(payload.get("skip_existing")),
        "source": payload.get("source", "pubmed"),
        "extra_query": (payload.get("extra_query") or "").strip(),
        "workers": max(1, min(int(payload.get("workers") or 8), 16)),
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaigning Data Extractor</title>
<style>
  :root {
    --paper: #eef0f3;
    --card: #ffffff;
    --ink: #17212b;
    --muted: #63707e;
    --line: #d3d9e0;
    --accent: #0f6b63;
    --accent-soft: #e2efed;
    --amber: #a86a12;
    --radius: 3px;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header {
    background: var(--ink); color: #f4f6f8;
    padding: 18px 28px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  }
  header h1 { margin: 0; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }
  header p { margin: 0; color: #9fb0bf; font-size: 13.5px; }
  main {
    display: grid; grid-template-columns: 370px minmax(0, 1fr);
    gap: 22px; padding: 22px 28px 60px; align-items: start;
  }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; padding: 16px; } }

  .panel { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); }
  .panel > h2 {
    margin: 0; padding: 13px 18px; font-size: 14px; font-weight: 600;
    border-bottom: 1px solid var(--line);
  }
  .panel > div { padding: 18px; }

  label { display: block; font-size: 13px; font-weight: 550; margin-bottom: 5px; }
  .hint { font-size: 12.5px; color: var(--muted); margin: 4px 0 0; }
  input[type=text], input[type=number], textarea {
    width: 100%; padding: 8px 10px; font: inherit; color: var(--ink);
    background: #fbfcfd; border: 1px solid var(--line); border-radius: var(--radius);
  }
  textarea { font-family: var(--mono); font-size: 13px; resize: vertical; }
  input:focus-visible, textarea:focus-visible, button:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 1px;
  }
  select:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  select { width: 100%; min-height: 116px; padding: 6px; font: inherit; color: var(--ink);
    background: #fbfcfd; border: 1px solid var(--line); border-radius: var(--radius); }
  .field { margin-bottom: 16px; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .check { display: flex; gap: 9px; align-items: flex-start; margin-bottom: 10px; font-size: 13.5px; }
  .check input { margin: 3px 0 0; flex: none; }
  .check span { color: var(--muted); display: block; font-size: 12.5px; }
  details { margin-top: 6px; border-top: 1px solid var(--line); padding-top: 12px; }
  summary { cursor: pointer; font-size: 13px; color: var(--accent); }
  details[open] > summary { margin-bottom: 12px; }

  .actions { display: flex; gap: 10px; margin-top: 6px; }
  button {
    font: inherit; font-weight: 550; padding: 9px 16px; border-radius: var(--radius);
    border: 1px solid transparent; cursor: pointer;
  }
  .primary { background: var(--accent); color: #fff; }
  .primary:hover { background: #0c564f; }
  .ghost { background: #fff; border-color: var(--line); color: var(--ink); }
  .ghost:hover { border-color: #aab4bf; }
  button[disabled] { opacity: .45; cursor: not-allowed; }

  .status { display: flex; gap: 26px; flex-wrap: wrap; padding: 16px 18px; border-bottom: 1px solid var(--line); }
  .metric span { display: block; font-size: 12.5px; color: var(--muted); }
  .metric strong { font: 600 22px/1.2 var(--mono); }
  .state { font-size: 13px; padding: 10px 18px; border-bottom: 1px solid var(--line); color: var(--muted); }
  .state.run { background: #fdf6e9; color: var(--amber); }
  .state.done { background: var(--accent-soft); color: #0b4f49; }
  .state.err { background: #fbeceb; color: #9a2d23; }
  .state.verify { background: #eef2fa; color: #23539a; }
  .querybox { font-family: var(--mono); font-size: 12.5px; color: var(--muted);
    padding: 10px 18px; border-bottom: 1px solid var(--line); word-break: break-word; }

  .tablewrap { overflow-x: auto; max-height: 62vh; overflow-y: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e7eaee; vertical-align: top; }
  th { position: sticky; top: 0; background: #f7f8fa; font-weight: 600; z-index: 1; }
  td.mail { font-family: var(--mono); font-size: 12.5px; white-space: nowrap; }
  td.title { max-width: 420px; }
  .empty { padding: 42px 18px; color: var(--muted); text-align: center; }
  .foot { padding: 14px 18px; display: flex; justify-content: space-between; gap: 12px;
    align-items: center; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>PubMed author contacts</h1>
  <p>Search by keyword and year, keep high-income-country affiliations, export a CSV.</p>
</header>

<main>
  <section class="panel">
    <h2>Search</h2>
    <div>
      <div class="field">
        <label for="keyword">Keywords</label>
        <input type="text" id="keyword" placeholder="e.g. crispr AND sickle cell" autofocus>
        <p class="hint">PubMed syntax works here: AND, OR, quotes, [Title], [MeSH Terms].</p>
      </div>

      <div class="field pair">
        <div>
          <label for="yfrom">From year</label>
          <input type="number" id="yfrom" value="2023" min="1800" max="2100">
        </div>
        <div>
          <label for="yto">To year</label>
          <input type="number" id="yto" value="2026" min="1800" max="2100">
        </div>
      </div>
      
      <div class="field">
        <label for="source">Source</label>
        <select id="source" style="min-height: auto;">
          <option value="pubmed">PubMed</option>
          <option value="epmc">Europe PMC</option>
          <option value="mdpi">MDPI (Open Access Journals)</option>
        </select>
      </div>

      <div class="field">
        <label for="limit">Articles to scan</label>
        <input type="number" id="limit" value="1000" min="1" max="100000" step="500">
        <p class="hint">Scan up to 100,000 articles.</p>
      </div>

      <div class="field">
        <label for="countries">Specific Countries</label>
        <input type="text" id="countries" placeholder="e.g. United States, Germany (comma separated)">
        <p class="hint">Only retrieve data for these countries. Leave blank for all.</p>
      </div>

      <label class="check">
        <input type="checkbox" id="high" checked>
        <div>High-income countries only
          <span>World Bank classification, read from the author's affiliation.</span></div>
      </label>
      <label class="check" style="display:none">
        <input type="checkbox" id="fulltext" checked>
      </label>
      <label class="check">
        <input type="checkbox" id="oa">
        <div>Open-access articles only</div>
      </label>

      <details>
        <summary>More options</summary>
        <label class="check">
          <input type="checkbox" id="generic" checked>
          <div>Skip publisher and office addresses
            <span>Drops permissions@, editorial@ and similar.</span></div>
        </label>
        <label class="check">
          <input type="checkbox" id="skip_existing">
          <div>Skip previously exported data
            <span>Exclude emails already saved in the exports folder.</span></div>
        </label>
        <label class="check">
          <input type="checkbox" id="unknown">
          <div>Keep rows where the country can't be read</div>
        </label>
        <label class="check">
          <input type="checkbox" id="english">
          <div>English-language articles only</div>
        </label>
        <div class="field" style="margin-top:12px">
          <label for="extra">Extra query filter</label>
          <input type="text" id="extra" placeholder='e.g. SRC:"MED" OR JOURNAL:"Nature"'>
        </div>
      </details>

      <div class="actions">
        <button class="primary" id="run">Start extraction</button>
        <button class="ghost" id="stop" disabled>Stop</button>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>Results</h2>
    <div class="status">
      <div class="metric"><span>Matching articles</span><strong id="m-hits">—</strong></div>
      <div class="metric"><span>Scanned</span><strong id="m-scanned">0</strong></div>
      <div class="metric"><span>Contacts found</span><strong id="m-rows">0</strong></div>
      <div class="metric"><span>Elapsed</span><strong id="m-time">0s</strong></div>
    </div>
    <div class="state" id="state">Set your keywords and years, then start the extraction.</div>
    <div class="querybox" id="querybox" hidden></div>
    <div class="tablewrap"><div class="empty" id="empty">No results yet.</div>
      <table id="table" hidden><thead><tr>
        <th>Title</th><th>Author</th><th>Email</th><th>Country</th><th>Year</th>
      </tr></thead><tbody id="tbody"></tbody></table>
    </div>
    <div class="foot">
      <span id="footnote">Preview shows the first 200 rows; the CSV holds everything.</span>
      <div style="display:flex; gap:10px;">
        <button class="ghost" id="verify" disabled>Verify & Deduplicate</button>
        <button class="primary" id="download" disabled>Download CSV</button>
      </div>
    </div>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let jobId = null, timer = null;

function settings() {
  const countriesStr = $('countries').value.trim();
  const selected_countries = countriesStr ? countriesStr.split(',').map(s => s.trim()).filter(s => s) : [];
  return {
    keyword: $('keyword').value,
    year_from: Number($('yfrom').value),
    year_to: Number($('yto').value),
    limit: Number($('limit').value),
    high_income_only: $('high').checked,
    selected_countries: selected_countries,
    use_fulltext: $('fulltext') ? $('fulltext').checked : true,
    open_access_only: $('oa').checked,
    drop_generic: $('generic').checked,
    skip_existing: $('skip_existing') ? $('skip_existing').checked : false,
    keep_unknown_country: $('unknown').checked,
    english_only: $('english').checked,
    source: $('source').value,
    extra_query: $('extra').value
  };
}

function setState(text, kind) {
  const el = $('state');
  el.textContent = text;
  el.className = 'state' + (kind ? ' ' + kind : '');
}

async function start() {
  const res = await fetch('/api/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(settings())
  });
  const data = await res.json();
  if (!res.ok) { setState(data.error, 'err'); return; }
  jobId = data.id;
  $('run').disabled = true; $('stop').disabled = false; $('download').disabled = true; $('verify').disabled = true;
  $('tbody').innerHTML = ''; $('table').hidden = true; $('empty').hidden = false;
  const srcNames = {'epmc': 'Europe PMC', 'mdpi': 'MDPI', 'pubmed': 'PubMed'};
  const srcName = srcNames[$('source').value] || 'PubMed';
  $('empty').textContent = 'Searching ' + srcName + '…';
  setState('Searching…', 'run');
  timer = setInterval(poll, 800);
}

async function poll() {
  if (!jobId) return;
  const res = await fetch('/api/status?id=' + jobId);
  const s = await res.json();
  $('m-hits').textContent = s.hit_count === null ? '—' : s.hit_count.toLocaleString();
  $('m-scanned').textContent = s.processed.toLocaleString();
  $('m-rows').textContent = s.rows.toLocaleString();
  $('m-time').textContent = s.elapsed + 's';
  if (s.query) { $('querybox').hidden = false; $('querybox').textContent = s.query; }

  if (s.stage === 'queued') {
    setState('Starting…', 'run');
    return;
  }
  const srcNames = {'epmc': 'Europe PMC', 'mdpi': 'MDPI', 'pubmed': 'PubMed'};
  const srcName = srcNames[s.source] || 'PubMed';
  if (s.stage === 'searching' && s.processed === 0) {
    setState('Connecting to ' + srcName + '… (' + s.elapsed + 's — if this runs past ~30s, ' +
      'check the server console window for a [' + s.source + '] log line)', 'run');
    return;
  }
  if (s.stage === 'searching' || s.stage === 'fetching') {
    setState('Reading articles… ' + s.processed + ' scanned, ' + s.rows + ' contacts so far.', 'run');
    return;
  }
  
  if (s.stage === 'verifying') {
    setState('Verifying domains and removing duplicates...', 'verify');
    $('verify').disabled = true;
    $('download').disabled = true;
    timer = setInterval(poll, 800);
    return;
  }

  clearInterval(timer); timer = null;
  $('run').disabled = false; $('stop').disabled = true;

  if (s.stage === 'error') { setState('Extraction failed — ' + s.error, 'err'); return; }

  const st = s.stats || {};
  let msg = (s.stage === 'stopped' ? 'Stopped early. ' : 'Finished. ') +
    s.rows + ' contacts from ' + (st.with_email || 0) + ' of ' + s.processed +
    ' articles. Saved to ' + s.csv_path;
  if (s.stopped_early) {
    msg += '  Note: ' + s.stopped_early + ' Try narrowing the keyword/year range, ' +
      'or re-run — this is a pagination limit, not an error in this tool.';
  }
  setState(msg, s.stopped_early ? 'err' : 'done');
  $('download').disabled = s.rows === 0;
  $('verify').disabled = s.rows === 0;
  loadPreview();
}

async function loadPreview() {
  const res = await fetch('/api/preview?id=' + jobId);
  const data = await res.json();
  const body = $('tbody');
  body.innerHTML = '';
  if (!data.rows.length) {
    $('table').hidden = true; $('empty').hidden = false;
    $('empty').textContent = 'No contacts matched. Try a broader keyword, a wider year range, or turn off the high-income filter.';
    return;
  }
  for (const r of data.rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="title"></td><td></td><td class="mail"></td><td></td><td></td>';
    tr.children[0].textContent = r.title;
    tr.children[1].textContent = r.author_name || '—';
    tr.children[2].textContent = r.email;
    tr.children[3].textContent = r.country;
    tr.children[4].textContent = r.pub_year;
    body.appendChild(tr);
  }
  $('empty').hidden = true; $('table').hidden = false;
  $('footnote').textContent = 'Showing ' + data.rows.length + ' of ' + data.total + ' rows.';
}

$('run').onclick = start;
$('stop').onclick = async () => {
  if (jobId) { await fetch('/api/stop?id=' + jobId, {method: 'POST'}); setState('Stopping…', 'run'); }
};
$('verify').onclick = async () => {
  if (jobId) {
    await fetch('/api/verify?id=' + jobId, {method: 'POST'});
    setState('Verifying domains and removing duplicates...', 'verify');
    $('verify').disabled = true;
    $('download').disabled = true;
    timer = setInterval(poll, 800);
  }
};
$('download').onclick = () => { if (jobId) window.location = '/api/csv?id=' + jobId; };
$('keyword').addEventListener('keydown', e => { if (e.key === 'Enter') start(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_cli(args):
    settings = normalise_settings({
        "keyword": args.keyword, "year_from": args.year_from,
        "year_to": args.year_to, "limit": args.limit,
        "source": args.source,
        "open_access_only": args.open_access, "use_fulltext": not args.no_fulltext,
        "high_income_only": not args.all_countries,
        "keep_unknown_country": args.keep_unknown,
    })
    state = {"last": 0}

    def progress(**fields):
        if "processed" in fields and time.time() - state["last"] > 1:
            state["last"] = time.time()
            print("  scanned %s articles, %s contacts" % (fields["processed"], fields.get("rows", 0)))
        if "query" in fields:
            print("Query: %s" % fields["query"])
        if fields.get("hit_count") is not None:
            print("Matching articles: %s" % fields["hit_count"])

    source = settings.get("source", "pubmed")
    if source == "pubmed":
        rows, stats = pubmed_extractor.run_extraction(settings, progress, lambda: False)
    elif source == "epmc":
        rows, stats = epmc_extractor.run_extraction(settings, progress, lambda: False)
    elif source == "mdpi":
        rows, stats = mdpi_extractor.run_extraction(settings, progress, lambda: False)
    else:
        raise ValueError("Unknown source: %s" % source)
    out = args.out or os.path.join(EXPORT_DIR, "%s-%s-%s-%s.csv" % (
        source, safe_slug(args.keyword), args.year_from, args.year_to))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(rows_to_csv(rows))
    print("\n%d contacts from %d articles -> %s" % (len(rows), stats["articles"], out))


def main():
    global SERVER_PORT, SERVER_HOST
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", action="store_true", help="run once without the web UI")
    parser.add_argument("--keyword")
    parser.add_argument("--from", dest="year_from", type=int)
    parser.add_argument("--to", dest="year_to", type=int)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source", choices=["pubmed", "epmc", "mdpi"], default="pubmed")
    parser.add_argument("--out")
    parser.add_argument("--open-access", action="store_true")
    parser.add_argument("--no-fulltext", action="store_true")
    parser.add_argument("--all-countries", action="store_true")
    parser.add_argument("--keep-unknown", action="store_true")
    parser.add_argument("--host", default="0.0.0.0", help="host interface to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.cli:
        if not (args.keyword and args.year_from and args.year_to):
            parser.error("--cli needs --keyword, --from and --to")
        return run_cli(args)

    SERVER_PORT = args.port
    SERVER_HOST = args.host
    address = (args.host, args.port)
    server = ThreadingHTTPServer(address, Handler)
    
    lan_ip = get_local_ip()
    url_local = "http://127.0.0.1:%d" % args.port
    url_lan = "http://%s:%d" % (lan_ip, args.port)

    print("=================================================================")
    print("  TargetPulse — Academic Outreach & Contact Discovery Studio")
    print("=================================================================")
    print("  Local Access (This PC):    %s" % url_local)
    if lan_ip != "127.0.0.1":
        print("  Office Wi-Fi / LAN Share:  %s" % url_lan)
    print("  Saved Exports Directory:   %s" % EXPORT_DIR)
    print("=================================================================")

    print("Checking repository connectivity...")
    ok, message = pubmed_extractor.check_connectivity()
    print("  PubMed:    %s (%s)" % ("ONLINE" if ok else "OFFLINE", message))
    
    ok, message = epmc_extractor.check_connectivity()
    print("  EuropePMC: %s (%s)" % ("ONLINE" if ok else "OFFLINE", message))

    ok, message = mdpi_extractor.check_connectivity()
    print("  MDPI:      %s (%s)" % ("ONLINE" if ok else "OFFLINE", message))
    print("-----------------------------------------------------------------")
    print("Press Ctrl+C to stop the server at any time.\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url_local)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTargetPulse stopped cleanly.")


if __name__ == "__main__":
    main()