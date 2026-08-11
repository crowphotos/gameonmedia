#!/usr/bin/env python3
"""
email_to_wp_drafts.py
---------------------
Reads matching Gmail messages and creates WordPress DRAFT posts via the REST API.
Rule-based (no AI). Each email -> one draft. Nothing is published automatically.

SETUP (one time):
  1. pip install requests beautifulsoup4 google-auth google-auth-oauthlib google-api-python-client
  2. Gmail API: create an OAuth "Desktop app" client in Google Cloud Console,
     download the JSON, save it next to this script as `credentials.json`.
     Publish the app (In production) so refresh tokens don't expire every 7 days.
  3. WordPress: WP admin -> Users -> Profile -> Application Passwords -> add one,
     copy the value. Put it (and your settings) in the CONFIG block below.

RUN:
  python3 email_to_wp_drafts.py            # process new matching emails
  python3 email_to_wp_drafts.py --dry-run  # show what WOULD be posted, create nothing
"""

import os
import re
import sys
import json
import base64
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Load secrets from a local .env file sitting next to this script.
HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))


def _require(name):
    """Fetch a required env var or fail loudly with a helpful message."""
    val = os.getenv(name)
    if not val:
        sys.exit(f"Missing required setting '{name}'. "
                 f"Copy .env.example to .env and fill it in.")
    return val


# =================== CONFIG ===================
# Secrets & per-install values come from .env (NOT committed to git).
# Logic lists below stay in the script (safe to commit, version-controlled).

# --- From .env ---
WP_SITE_URL     = _require("WP_SITE_URL").rstrip("/")
WP_USERNAME     = _require("WP_USERNAME")
WP_APP_PASSWORD = _require("WP_APP_PASSWORD")
GMAIL_QUERY     = _require("GMAIL_QUERY")
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "News")  # optional, defaults to News

# --- Artist watchlist for tagging (logic — kept in repo) ---
# Any of these names found in the email body becomes a post tag.
# Matching is case-insensitive, whole-word. Add yours here.
ARTIST_WATCHLIST = [
    "Rush", "Sammy Hagar", "Pat Benatar", "Joan Jett",
    "Metallica", "Iron Maiden", "Megadeth",
    "Chris Stapleton", "Lainey Wilson", "Cody Johnson",
]

# --- Acronyms that always stay uppercase in titles (logic — kept in repo) ---
TITLE_ACRONYMS = {
    "NYC", "USA", "US", "UK", "LA", "SD", "DC", "VIP", "DJ", "MC",
    "EP", "LP", "CD", "TV", "PR", "AC/DC", "KSON", "SDSU", "NFL", "MLB",
}

# --- Browser-like User-Agent so mod_security doesn't 406 the requests ---
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

# --- Files ---
CREDS_FILE  = os.path.join(HERE, "credentials.json")   # Google OAuth client
TOKEN_FILE  = os.path.join(HERE, "token.json")         # cached after first run

# Runtime state lives in logs/ to keep the project root clean.
LOGS_DIR    = os.path.join(HERE, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
SEEN_FILE   = os.path.join(LOGS_DIR, "processed_ids.json")  # dedupe: don't repost

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ==================================================================


TITLE_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of",
    "on", "or", "per", "the", "to", "v", "vs", "via", "with", "from",
}


def title_case(title):
    """Headline-style Title Case. Lowercases small words (except first/last),
    keeps known acronyms uppercase, and de-shouts all-caps subjects."""
    words = title.split()
    if not words:
        return title
    n = len(words)
    letters = re.sub(r"[^A-Za-z]", "", title)
    whole_all_caps = letters.isupper() and len(letters) > 1

    out = []
    for i, w in enumerate(words):
        core = re.sub(r"[^A-Za-z/]", "", w).upper()
        # Always-uppercase acronyms
        if core in TITLE_ACRONYMS:
            out.append(w.upper())
            continue
        if not whole_all_caps:
            # Preserve genuine acronyms / internal caps when title isn't shouting
            bare = re.sub(r"[^A-Za-z]", "", w)
            if bare and bare.isupper() and len(bare) > 1:
                out.append(w)
                continue
            if re.search(r"[A-Z]", w[1:]):
                out.append(w)
                continue
        lw = w.lower()
        if 0 < i < n - 1 and lw in TITLE_SMALL_WORDS:
            out.append(lw)
        else:
            out.append("-".join(p[:1].upper() + p[1:] if p else p
                                 for p in lw.split("-")))
    return " ".join(out)


def log(msg):
    """Print with no timestamp — used for per-item detail lines."""
    print(msg)


def log_run_header(dry):
    """Write a dated banner at the start of every run so run.log is scannable."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = " [DRY RUN]" if dry else ""
    print(f"\n===== RUN {stamp}{mode} =====")


# ----------------------------- Gmail -----------------------------

def gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_message_ids(svc, query):
    ids, page = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=query, pageToken=page, maxResults=100
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def get_message(svc, msg_id):
    return svc.users().messages().get(userId="me", id=msg_id, format="full").execute()


def header(msg, name):
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def extract_body(payload):
    """Return (html, plain). Walks multipart trees, prefers text/html."""
    html, plain = "", ""

    def walk(part):
        nonlocal html, plain
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
            if mime == "text/html" and not html:
                html = decoded
            elif mime == "text/plain" and not plain:
                plain = decoded
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return html, plain


# --------------------------- Cleaning ----------------------------

DATE_LOCATION_PREFIX = re.compile(
    r"^\s*(?:[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}\s*[—\-–]\s*)?"
    r"(?:[A-Z][A-Za-z\.\s]+,\s*[A-Z]{2}\s*[—\-–:])\s*"
)

FOOTER_PATTERNS = [
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"\bview (this|in) (email|browser)\b", re.I),
    re.compile(r"\byou (are )?receiv(ed|ing) this\b", re.I),
    re.compile(r"\b(all rights reserved|©|\(c\))\b", re.I),
    re.compile(r"\bmanage (your )?(preferences|subscription)\b", re.I),
    re.compile(r"\bprivacy policy\b", re.I),
]

ABOUT_HEADING = re.compile(r"^\s*about\s+\S+", re.I)


def clean_html(raw_html):
    """Apply all the cleaning rules and return sanitized HTML."""
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style", "head", "meta", "title"]):
        tag.decompose()

    for tag in soup.find_all(["table", "thead", "tbody", "tfoot", "tr"]):
        tag.unwrap()
    for tag in soup.find_all(["td", "th"]):
        tag.name = "div"

    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        if ABOUT_HEADING.match(h.get_text(" ", strip=True)):
            nxt = h.find_next_sibling()
            h.decompose()
            while nxt and not re.match(r"^h[1-6]$", getattr(nxt, "name", "") or ""):
                follow = nxt.find_next_sibling()
                nxt.decompose()
                nxt = follow

    for el in soup.find_all(True):
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        if any(p.search(txt) for p in FOOTER_PATTERNS) and len(txt) < 400:
            el.decompose()

    for a in soup.find_all("a", href=True):
        a["target"] = "_blank"
        a["rel"] = "noopener noreferrer"

    first_text = soup.find(string=lambda s: isinstance(s, NavigableString) and s.strip())
    if first_text:
        new = DATE_LOCATION_PREFIX.sub("", str(first_text))
        if new != str(first_text):
            first_text.replace_with(new)

    for el in soup.find_all(["p", "div", "span"]):
        if not el.get_text(strip=True) and not el.find("img"):
            el.decompose()

    return str(soup).strip()


def first_sentence(html, max_len=155):
    """Plain-text first sentence(s) from cleaned HTML, capped at max_len for Yoast."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    # Grab up to the first sentence end; extend to next if very short.
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    desc = m.group(1) if m else text
    if len(desc) < 60:  # too short — pull a bit more context
        m2 = re.match(r"(.+?[.!?]\s+.+?[.!?])(\s|$)", text)
        if m2:
            desc = m2.group(1)
    if len(desc) > max_len:
        desc = desc[:max_len].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return desc


def find_artist_tags(text):
    found = []
    for name in ARTIST_WATCHLIST:
        if re.search(r"\b" + re.escape(name) + r"\b", text, re.I):
            found.append(name)
    return found


# -------------------------- WordPress ----------------------------

class WP:
    def __init__(self, site, user, app_pw):
        self.base = site.rstrip("/") + "/wp-json/wp/v2"
        self.auth = (user, app_pw.replace(" ", ""))
        self.headers = {"User-Agent": USER_AGENT}
        self._cat_cache = {}
        self._tag_cache = {}

    def _term_id(self, kind, name, cache):
        if name in cache:
            return cache[name]
        r = requests.get(f"{self.base}/{kind}", params={"search": name},
                         auth=self.auth, headers=self.headers, timeout=30)
        r.raise_for_status()
        for t in r.json():
            if t["name"].lower() == name.lower():
                cache[name] = t["id"]
                return t["id"]
        r = requests.post(f"{self.base}/{kind}", json={"name": name},
                          auth=self.auth, headers=self.headers, timeout=30)
        r.raise_for_status()
        tid = r.json()["id"]
        cache[name] = tid
        return tid

    def category_id(self, name):
        return self._term_id("categories", name, self._cat_cache)

    def tag_id(self, name):
        return self._term_id("tags", name, self._tag_cache)

    def create_draft(self, title, html, category, tags):
        focus_kw = title.replace(",", "").strip()  # Yoast treats commas as phrase separators
        metadesc = first_sentence(html) or title.strip()  # fall back to title if body empty
        payload = {
            "title": title or "(no subject)",
            "content": html,
            "status": "draft",
            "categories": [self.category_id(category)] if category else [],
            "tags": [self.tag_id(t) for t in tags],
            "meta": {
                "_yoast_wpseo_focuskw": focus_kw,
                "_yoast_wpseo_metadesc": metadesc,
            },
        }
        r = requests.post(f"{self.base}/posts", json=payload,
                          auth=self.auth, headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()


# ----------------------------- Main ------------------------------

def load_seen():
    if os.path.exists(SEEN_FILE):
        return set(json.load(open(SEEN_FILE)))
    return set()


def save_seen(seen):
    json.dump(sorted(seen), open(SEEN_FILE, "w"), indent=2)


def main():
    dry = "--dry-run" in sys.argv
    log_run_header(dry)

    try:
        svc = gmail_service()
    except Exception as e:
        log(f"!! Gmail auth failed: {e}")
        log("   If this says invalid_grant, delete token.json and re-run manually to re-authorize.")
        sys.exit(1)

    seen = load_seen()
    ids = list_message_ids(svc, GMAIL_QUERY)
    new_ids = [i for i in ids if i not in seen]

    log(f"Matched {len(ids)} message(s); {len(new_ids)} new to process.")

    wp = None if dry else WP(WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD)
    created = 0

    for msg_id in new_ids:
        msg = get_message(svc, msg_id)
        subject = title_case(header(msg, "Subject").strip())
        raw_html, plain = extract_body(msg["payload"])

        if raw_html:
            content = clean_html(raw_html)
        else:
            content = "".join(f"<p>{l}</p>" for l in plain.splitlines() if l.strip())

        text_for_tags = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
        tags = find_artist_tags(text_for_tags + " " + subject)

        log(f"• {subject!r}  | tags: {tags or '—'} | chars: {len(content)}")

        if dry:
            continue

        try:
            post = wp.create_draft(subject, content, DEFAULT_CATEGORY, tags)
            log(f"  -> draft #{post['id']} created")
            seen.add(msg_id)
            created += 1
        except requests.HTTPError as e:
            log(f"  !! WP error: {e} — {getattr(e.response,'text','')[:200]}")

    if not dry:
        save_seen(seen)
        log(f"Result: {created} draft(s) created. Review in WP admin before publishing.")
    else:
        log("Dry run complete — nothing created.")


if __name__ == "__main__":
    main()
