#!/usr/bin/env python3
"""Scrape upcoming committee hearings (and best-effort witness-slip counts) for
the jurisdictions the Govbot Public Hearings site shows.

Unlike the bill dataset (OpenStates -> git-repos-as-datasets), committee hearings
are *live* artifacts published by each statehouse on its own machine-readable
endpoint. govbot does not get them from OpenStates, so this action taps the
sources directly:

* Illinois     -> ilga.gov Hearings JSON API (per chamber, date range)
* Washington   -> leg.wa.gov CommitteeMeetingService SOAP/XML
* Massachusetts -> malegislature.gov Hearings JSON API (list + per-hearing detail)
* Alaska        -> akleg.gov BASIS meetings JSON API (one document per legislature)
* USA (Federal) -> federalregister.gov API: rules whose public comment period is open
* Chicago       -> the City Clerk's eLMS API (council + committee meetings)
* Every other state -> its legislature's own website, read by Open States' open-source
                   meeting scrapers that the refresh workflow runs (--os-events)

Every meeting is also kept in data/ (--data-dir: one JSON file each, history included) and
published as CSV + iCalendar downloads (--downloads-dir site/downloads).

The output is one document matching schemas/govbot.hearings.schema.json, written to
site/hearings.json. The refresh workflow runs this every 12 hours, so the site shows
near-future committee activity that Govbot's daily bill snapshot cannot.

Design rules (see CLAUDE.md):
* Only the Python standard library — no scraper deps, runs in a bare CI step.
* Fail loudly, recover gracefully: any source that errors degrades to zero
  hearings for that jurisdiction, never a crash. A caller that gets an empty
  document keeps the committed sample instead (see .github/workflows/refresh.yml).
* Pure parsers (parse_*) take raw text and are exercised offline by snapshot
  fixtures under __snapshots__/; only the fetch_* helpers touch the network.
* Witness-slip COUNTS are best-effort. Many capitols only expose counts while a
  slip window is open (a canceled hearing's slip page returns an error page), so
  slips are optional per the schema and their absence is normal. The reliable,
  always-useful signal is the hearing itself plus a deep link to file.

Usage:
    # Live (what the refresh workflow runs):
    python3 scraper/main.py --jurisdictions il,wa,ma,ak,us \
        --output site/hearings.json

    # Offline, deterministic — rebuild the snapshot from fixtures:
    python3 scraper/main.py --from-fixtures scraper/__snapshots__/raw \
        --now 2026-08-28T00:00:00Z --output -
"""

import argparse
import html
import http.client
import json
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
FETCH_TIMEOUT = 15
# Illinois General Assembly id for the current (104th) GA and its session id,
# as used by ilga.gov's own Hearings API and witness-slip URLs.
IL_GA_ID = 18
IL_SESSION_ID = 114
# Rolling window: from today through ~6 weeks out. Only upcoming hearings are
# shown (past ones are filtered in assemble), and committee calendars rarely
# post further than this.
WINDOW_BACK_DAYS = 0
WINDOW_FWD_DAYS = 45

# Jurisdiction participation portals (where a resident actually files/​signs in).
JURISDICTIONS = {
    "il": {
        "code": "il", "name": "Illinois", "participation": "witness_slip",
        "portal_url": "https://my.ilga.gov/",
    },
    "wa": {
        "code": "wa", "name": "Washington", "participation": "committee_sign_in",
        "portal_url": "https://app.leg.wa.gov/csi/",
    },
    "ma": {
        "code": "ma", "name": "Massachusetts", "participation": "written_testimony",
        "portal_url": "https://malegislature.gov/",
    },
    "ak": {
        "code": "ak", "name": "Alaska", "participation": "public_comment",
        "portal_url": "https://www.akleg.gov/poms/",
    },
    "us": {
        "code": "us", "name": "USA (Federal)", "participation": "public_comment",
        "portal_url": "https://www.regulations.gov/",
    },
    # Congress's committee hearings. "usa" is Govbot's code for federal bills, so the bills on
    # each agenda pick up Govbot's titles; "us" stays the Federal Register's comment periods.
    "usa": {
        "code": "usa", "name": "U.S. Congress", "participation": "contact_only",
        "portal_url": "https://www.congress.gov/members/find-your-member",
        # "See all" goes to the Library of Congress's schedule of every committee meeting.
        "full_list_url": "https://www.congress.gov/committee-schedule",
        "how_to": ("Congressional hearings hear from invited witnesses. You weigh in by contacting "
                   "your representative and senators, who sit on the committees; some committees "
                   "also accept written statements from the public for the hearing record (each "
                   "committee's site says how)."),
    },
    # The first city. A 3-letter code so it can never clash with a state's.
    "chi": {
        "code": "chi", "name": "Chicago City Council", "participation": "public_comment",
        "portal_url": ("https://www.chicityclerk.com/about-city-government-chicago-city-council"
                       "#ChicagoCityCouncilPublicCommentProcess"),
        # "See all" goes to the Clerk's own meeting list, not the comment rules.
        "full_list_url": "https://chicityclerkelms.chicago.gov/Meetings/",
    },
}

BILL_RE = re.compile(r"\b(HJR|SJR|HB|SB|HR|SR)\s*-?\s*(\d{1,5})\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# network (thin, testable-around)
# --------------------------------------------------------------------------- #
# Incomplete certificate chains. ilga.gov (as of Nov 2026) sends only its own certificate and
# leaves out the intermediate that links it to a trusted root. Browsers download the missing
# intermediate from the address printed in the site's certificate (its "CA Issuers" field) and
# carry on; Python just refuses the site, so Illinois came back empty. We do what the browser
# does, without weakening anything (the same fix as Govbot's scripts/fetch_sponsor_photos.py):
#   1. read (never trust) the site's certificate to find that address,
#   2. download the intermediate from the certificate authority,
#   3. retry with Python's normal checks plus that one extra certificate, and partial-chain
#      trust OFF, so the chain must still reach a root the machine already trusts (a forged
#      "intermediate" fails) and the hostname is still checked.
# Only used after an "unable to get local issuer certificate" failure; any problem leaves the
# fetch failed, exactly as before.
_CA_ISSUERS_OID = b"\x06\x08\x2b\x06\x01\x05\x05\x07\x30\x02"   # id-ad-caIssuers
_MISSING_ISSUER = 20      # X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY
_aia_contexts = {}        # host -> verified SSLContext, or None when it can't be fixed


def ca_issuers_url(der):
    """The "CA Issuers" URL from a DER certificate's Authority Information Access
    extension (where to download the certificate that signed it), or None."""
    i = der.find(_CA_ISSUERS_OID + b"\x86")       # [6] uniformResourceIdentifier
    if i < 0:
        return None
    j = i + len(_CA_ISSUERS_OID) + 1
    if j >= len(der) or der[j] >= 0x80:            # URLs this short use the 1-byte length form
        return None
    url = der[j + 1:j + 1 + der[j]].decode("ascii", "replace")
    return url if url.startswith(("http://", "https://")) else None


def verified_context_with(intermediate_der):
    """Python's default verified TLS context plus one intermediate certificate, with
    partial-chain trust off so the intermediate is only a link, never a root of trust."""
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(intermediate_der))
    return ctx


def _peer_leaf_der(host, timeout):
    """The server's own certificate, read without verifying it: used only to find the
    CA Issuers address, never to trust or download anything from this connection."""
    peek = ssl.create_default_context()
    peek.check_hostname = False
    peek.verify_mode = ssl.CERT_NONE
    proxy = urllib.request.getproxies().get("https")
    if proxy and not urllib.request.proxy_bypass(host):
        pp = urllib.parse.urlsplit(proxy)
        conn = http.client.HTTPSConnection(pp.hostname, pp.port or 80, timeout=timeout, context=peek)
        conn.set_tunnel(host, 443)
    else:
        conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=peek)
    try:
        conn.connect()
        return conn.sock.getpeercert(binary_form=True)
    finally:
        conn.close()


def _aia_context(host, timeout):
    if host in _aia_contexts:
        return _aia_contexts[host]
    ctx = None
    try:
        url = ca_issuers_url(_peer_leaf_der(host, timeout) or b"")
        if url:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - CA's published cert
                data = r.read(65536)
            if data.startswith(b"-----BEGIN CERTIFICATE"):
                data = ssl.PEM_cert_to_DER_cert(data.decode("ascii", "replace"))
            if data[:1] == b"\x30":                 # a DER certificate (PKCS#7 bundles aren't handled)
                ctx = verified_context_with(data)
    except OSError as exc:  # a network blip: don't remember it, so the next request retries
        print(f"warning: could not complete the certificate chain for {host}: {exc}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001 - fail-soft
        print(f"warning: could not complete the certificate chain for {host}: {exc}", file=sys.stderr)
    _aia_contexts[host] = ctx
    return ctx


def _open(req, timeout):
    """urlopen, plus the browser-style missing-intermediate fallback above."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - https APIs
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        host = urllib.parse.urlsplit(req.full_url).hostname
        if not (isinstance(reason, ssl.SSLCertVerificationError)
                and reason.verify_code == _MISSING_ISSUER and host):
            raise
        ctx = _aia_context(host, timeout)
        if ctx is None:
            raise
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        return opener.open(req, timeout=timeout)


def fetch_text(url, timeout=FETCH_TIMEOUT, extra_headers=None):
    """GET a URL, returning the body text or None on any failure."""
    headers = {
        "user-agent": UA,
        "accept": "application/json,text/xml,application/xml,text/html;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with _open(req, timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", "replace")
    except Exception as err:  # network, TLS, timeout, decode — all non-fatal
        print(f"warning: fetch failed {url}: {err}", file=sys.stderr)
        return None


def fetch_text_headers(url, extra_headers, timeout=FETCH_TIMEOUT):
    """fetch_text with extra request headers (some APIs want a version header)."""
    return fetch_text(url, timeout=timeout, extra_headers=extra_headers)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def extract_bill_ids(text):
    """Compact bill ids referenced in a subject/agenda line, de-duplicated."""
    seen = {}
    for m in BILL_RE.finditer(text or ""):
        seen[f"{m.group(1).upper()}{int(m.group(2))}"] = None
    return list(seen)


def join_location(*parts):
    return " · ".join(p.strip() for p in parts if p and p.strip())


# --------------------------------------------------------------------------- #
# Illinois — ilga.gov Hearings JSON API
# --------------------------------------------------------------------------- #
def il_hearings_url(chamber, begin, end):
    cid = "h" if chamber == "house" else "s"
    return (f"https://ilga.gov/API/Hearings/GetHearingsListByRange"
            f"?ChamberId={cid}&GaId={IL_GA_ID}"
            f"&BeginDate={begin:%-m/%-d/%Y}&EndDate={end:%-m/%-d/%Y}")


def il_bill_status_url(bill_id):
    """The official Bill Status page for a bill — a reliable, always-200 page
    that carries the working "Witness Slips" button. We link here rather than
    to the WitnessSlips deep link, which returns an error page (HTTP 500) when
    hit directly without a live session."""
    m = re.match(r"([A-Z]+)(\d+)", bill_id)
    if not m:
        return "https://my.ilga.gov/"
    doctype, num = m.group(1), m.group(2)
    return (f"https://ilga.gov/legislation/BillStatus"
            f"?DocTypeID={doctype}&DocNum={num}"
            f"&GAID={IL_GA_ID}&SessionID={IL_SESSION_ID}")


def il_slips_scrape_url(bill_id):
    """The witness-slip totals page (used only for best-effort count scraping,
    an opt-in step). It is not linked to users because it 500s when opened
    directly; see il_bill_status_url for the user-facing link."""
    m = re.match(r"([A-Z]+)(\d+)", bill_id)
    if not m:
        return "https://my.ilga.gov/"
    doctype, num = m.group(1), m.group(2)
    return (f"https://ilga.gov/Legislation/BillStatus/WitnessSlips"
            f"?GAID={IL_GA_ID}&DocNum={num}&DocTypeID={doctype}"
            f"&LegId=0&SessionID={IL_SESSION_ID}")


def il_details_url(chamber, committee_id, hearing_id):
    seg = "House" if chamber == "house" else "Senate"
    return f"https://ilga.gov/{seg}/hearings/details/{committee_id}/{hearing_id}"


def _il_time_to_iso(raw):
    """'8/31/2026 10:00 AM' -> '2026-08-31T10:00:00' (naive local), else None."""
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, AttributeError):
            continue
    return None


def parse_il_hearings(json_text, chamber):
    """Pure: normalize ilga.gov hearings JSON for one chamber into records."""
    try:
        rows = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        hearing_id = str(r.get("hearingID") or r.get("HearingID") or "")
        committee_id = str(r.get("committeeID") or r.get("CommitteeID") or "")
        if not hearing_id or not committee_id:
            continue
        subject = str(r.get("subjectMatter") or r.get("SubjectMatter") or "")
        committee = str(r.get("longDescription") or r.get("LongDescription")
                        or "Committee")
        text_line = str(r.get("textLine") or r.get("TextLine") or "")
        canceled = "cancel" in text_line.lower() or "cancel" in subject.lower()
        raw_when = str(r.get("scheduledDateTime") or r.get("ScheduledDateTime") or "")
        location = join_location(r.get("room") or r.get("Room") or "",
                                 r.get("building") or r.get("Building") or "",
                                 r.get("city") or r.get("City") or "")
        bills = [{"id": bid, "slips": None, "url": il_bill_status_url(bid)}
                 for bid in extract_bill_ids(subject + " " + committee)]
        out.append({
            "id": f"il-{chamber}-{committee_id}-{hearing_id}",
            "jurisdiction": "il",
            "chamber": chamber,
            "committee": committee,
            "title": subject,
            "scheduled_iso": _il_time_to_iso(raw_when),
            "scheduled_display": raw_when,
            "timezone": "America/Chicago",
            "location": location,
            "status": "canceled" if canceled else "scheduled",
            "bills": bills,
            "details_url": il_details_url(chamber, committee_id, hearing_id),
            "witness_slip_url": (bills[0]["url"] if bills
                                 else il_details_url(chamber, committee_id, hearing_id)),
            "source": "ilga.gov",
        })
    return out


def parse_slip_counts(html):
    """Pure, best-effort: aggregate proponent/opponent/no-position counts from a
    witness-slip page, or None when the page is an error / has no counts."""
    if not html or "General Assembly - Error" in html:
        return None
    def n(label):
        m = re.search(label + r"\s*:?\s*([\d,]+)", html, re.IGNORECASE)
        return int(m.group(1).replace(",", "")) if m else None
    prop, opp, none = n("Proponents?"), n("Opponents?"), n("No Position")
    if prop is None and opp is None and none is None:
        return None
    return {"proponents": prop or 0, "opponents": opp or 0, "no_position": none or 0}


def fetch_il(begin, end, with_slips):
    hearings = []
    for chamber in ("house", "senate"):
        text = fetch_text(il_hearings_url(chamber, begin, end))
        if text:
            hearings.extend(parse_il_hearings(text, chamber))
    if with_slips:
        enrich_il_slips(hearings)
    return hearings


def enrich_il_slips(hearings):
    """Best-effort: fill slip counts for bills of non-canceled hearings only,
    bounded and parallel. Failures leave slips as None (the schema allows it)."""
    targets = []
    for h in hearings:
        if h["status"] == "canceled":
            continue
        for b in h["bills"]:
            targets.append(b)
    if not targets:
        return
    def one(bill):
        html = fetch_text(il_slips_scrape_url(bill["id"]))
        counts = parse_slip_counts(html) if html else None
        if counts:
            bill["slips"] = counts
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, targets[:60]))  # cap: be polite to ilga.gov


# --------------------------------------------------------------------------- #
# Washington — leg.wa.gov CommitteeMeetingService (SOAP/XML)
# --------------------------------------------------------------------------- #
WA_NS = "{http://WSLWebServices.leg.wa.gov/}"


def _wa_text(el, tag):
    child = el.find(WA_NS + tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def wa_meetings_url(begin, end):
    return ("https://wslwebservices.leg.wa.gov/CommitteeMeetingService.asmx/"
            f"GetCommitteeMeetings?beginDate={begin:%Y-%m-%d}&endDate={end:%Y-%m-%d}")


def wa_items_url(agenda_id):
    return ("https://wslwebservices.leg.wa.gov/CommitteeMeetingService.asmx/"
            f"GetCommitteeMeetingItems?agendaId={agenda_id}")


def wa_chamber(agency):
    a = (agency or "").lower()
    if "senate" in a:
        return "senate"
    if "house" in a:
        return "house"
    return "other"


def parse_wa_meetings(xml_text):
    """Pure: normalize a GetCommitteeMeetings XML body into records (no bills)."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return []
    out = []
    for m in root.findall(WA_NS + "CommitteeMeeting"):
        agenda_id = _wa_text(m, "AgendaId")
        if not agenda_id:
            continue
        agency = _wa_text(m, "Agency")
        committee, committee_id = "Committee", ""
        committees = m.find(WA_NS + "Committees")
        if committees is not None:
            first = committees.find(WA_NS + "Committee")
            if first is not None:
                committee = _wa_text(first, "Name") or committee
                committee_id = _wa_text(first, "Id")
        when = _wa_text(m, "Date")  # already ISO-naive local
        canceled = _wa_text(m, "Cancelled").lower() == "true"
        location = join_location(_wa_text(m, "Room"), _wa_text(m, "Building"),
                                 _wa_text(m, "City"))
        notes = _wa_text(m, "Notes")
        chamber = wa_chamber(agency)
        out.append({
            "id": f"wa-{chamber}-{agenda_id}",
            "jurisdiction": "wa",
            "chamber": chamber,
            "committee": committee,
            "title": notes or f"{agency} · {committee}",
            "scheduled_iso": when or None,
            "scheduled_display": when,
            "timezone": "America/Los_Angeles",
            "location": location,
            "status": "canceled" if canceled else "scheduled",
            "bills": [],
            "details_url": f"https://app.leg.wa.gov/committeeschedules/Home/Agenda/{agenda_id}",
            "witness_slip_url": None,  # set by wa_attach_bills once the agenda is known
            "source": "leg.wa.gov",
            "_agenda_id": agenda_id,
            "_signin": (agency, committee_id),
        })
    return out


def wa_signin_url(agency, committee_id, agenda_id):
    """Committee Sign-In (CSI) opened on this exact meeting: CSI preselects the
    committee + meeting from these query parameters and lists its bills to sign in
    on. Its meeting ids are the web service's AgendaId and its committee ids the
    web service's Committee Id (checked against live CSI, 2026-10). None when CSI
    has no tab for the agency (CSI has Senate / House / Joint)."""
    if agency not in ("Senate", "House", "Joint") or not committee_id or not agenda_id:
        return None
    return (f"https://app.leg.wa.gov/csi/{agency}"
            f"?selectedCommittee={committee_id}&selectedMeeting={agenda_id}")


def wa_attach_bills(m, bill_ids):
    """Attach the agenda's bills and the sign-in link. Only a meeting with a public
    hearing on bills takes sign-in testimony; interim work sessions aren't listed in
    CSI at all, so they get no sign-in link (the agenda page stays linked)."""
    m["bills"] = wa_bills(bill_ids, m)
    if m["bills"]:
        m["witness_slip_url"] = wa_signin_url(*m["_signin"], m["_agenda_id"])
    m.pop("_agenda_id", None)
    m.pop("_signin", None)
    return m


def parse_wa_items(xml_text):
    """Pure: bill ids on a committee-meeting agenda."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return []
    ids = {}
    for item in root.findall(WA_NS + "CommitteeMeetingItem"):
        htype = (_wa_text(item, "HearingTypeDescription")
                 or _wa_text(item, "HearingType"))
        # Skip pure work sessions (not a public-testimony hearing).
        if htype and "work session" in htype.lower() and "public" not in htype.lower():
            continue
        bill = _wa_text(item, "BillId").replace(" ", "").upper()
        if bill:
            ids[bill] = None
    return list(ids)


def wa_bill_url(bill_id, year):
    """Official Washington bill-summary page for a bill id (e.g. HB1234)."""
    m = re.match(r"([A-Z]+)(\d+)", bill_id)
    if not m:
        return None  # the page falls back to the meeting's agenda
    return (f"https://app.leg.wa.gov/billsummary?BillNumber={m.group(2)}"
            f"&Year={year}&Initiative=false")


def _wa_year(hearing):
    iso = hearing.get("scheduled_iso") or ""
    return iso[:4] if iso[:4].isdigit() else "2025"


def wa_bills(ids, hearing):
    year = _wa_year(hearing)
    return [{"id": bid, "slips": None, "url": wa_bill_url(bid, year)} for bid in ids]


def fetch_wa(begin, end):
    text = fetch_text(wa_meetings_url(begin, end))
    if not text:
        return []
    meetings = parse_wa_meetings(text)  # keep canceled too; flagged in status
    def items(m):
        xml = fetch_text(wa_items_url(m["_agenda_id"]))
        wa_attach_bills(m, parse_wa_items(xml) if xml else [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(items, meetings))
    return meetings


# --------------------------------------------------------------------------- #
# Massachusetts — malegislature.gov Hearings JSON API
# --------------------------------------------------------------------------- #
# MA publishes a keyless JSON API. GET /api/Hearings lists every hearing of the
# current General Court as {EventId} stubs, ordered newest-scheduled first, so
# upcoming hearings cluster at the front. GET /api/Hearings/<id> returns the full
# record: committee, datetime (naive Eastern), location, and the bills on each
# agenda. We scan a bounded prefix of the list and keep the near-future ones.
MA_LIST_URL = "https://malegislature.gov/api/Hearings"
# Detail requests to pull from the (long, historical) list. Upcoming hearings sit
# at the top; a generous prefix catches them all while bounding the request count.
MA_SCAN_LIMIT = 150


def ma_detail_url(event_id):
    return f"https://malegislature.gov/api/Hearings/{event_id}"


def ma_hearing_page_url(event_id):
    return f"https://malegislature.gov/Events/Hearings/Detail/{event_id}"


def ma_bill_url(bill_number, general_court):
    return f"https://malegislature.gov/Bills/{general_court}/{bill_number}"


def _ma_chamber(name, code):
    """House / Senate / Joint from the committee name, falling back to the
    committee-code prefix (H/S/J), else 'other'."""
    text = (name or "").lower()
    if "joint" in text:
        return "joint"
    if "house" in text:
        return "house"
    if "senate" in text:
        return "senate"
    return {"J": "joint", "H": "house", "S": "senate"}.get((code or "")[:1].upper(), "other")


def _ma_location(loc):
    """A location a reader can act on: "Room 428A · 24 Beacon Street · Boston, MA"
    rather than a bare "428A · Boston". A room number/code is labeled "Room" so it
    reads as one; the State House street address and state are folded in from the
    API so the room isn't context-free."""
    if not isinstance(loc, dict):
        return ""
    room = (loc.get("LocationName") or "").strip()
    if room and room[0].isdigit():  # "428A"/"437" -> "Room 428A"; leave "Virtual …"
        room = "Room " + room
    city_state = ", ".join(p for p in [(loc.get("City") or "").strip(),
                                       (loc.get("State") or "").strip()] if p)
    return join_location(room, (loc.get("AddressLine1") or "").strip(), city_state)


def parse_ma_hearing_list(json_text):
    """Pure: ordered event ids from the /api/Hearings stub list."""
    try:
        rows = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    ids = []
    for r in rows if isinstance(rows, list) else []:
        eid = r.get("EventId") if isinstance(r, dict) else None
        if eid is not None:
            ids.append(eid)
    return ids


def parse_ma_hearing(json_text):
    """Pure: normalize one /api/Hearings/<id> detail into a hearing record, or
    None when the body isn't a usable hearing object. Tolerates the null-heavy
    records the API returns for stub/placeholder events."""
    try:
        d = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("EventId") is None:
        return None
    host = d.get("HearingHost") or {}
    code = host.get("CommitteeCode")
    general_court = host.get("GeneralCourtNumber")
    name = d.get("Name") or ""
    chamber = _ma_chamber(name, code)
    when = d.get("StartTime") or d.get("EventDate") or ""
    when = when.strip() if isinstance(when, str) else ""
    location = _ma_location(d.get("Location"))
    canceled = str(d.get("Status") or "").lower() == "canceled"
    # Bills: every document on every agenda, de-duplicated, normalized (H5516),
    # with the bill's title (MA is not in govbot's dataset, so the name shown next
    # to the number comes from MA's own feed rather than govbot enrichment).
    bills_by_num = {}
    for agenda in d.get("HearingAgendas") or []:
        for doc in (agenda or {}).get("DocumentsInAgenda") or []:
            num = str((doc or {}).get("BillNumber") or "").replace(" ", "").upper()
            if not num or num in bills_by_num:
                continue
            gc = (doc or {}).get("GeneralCourtNumber") or general_court or 194
            bill = {"id": num, "slips": None, "url": ma_bill_url(num, gc)}
            title = str((doc or {}).get("Title") or "").strip()
            if title:
                bill["title"] = title
            bills_by_num[num] = bill
    bills = list(bills_by_num.values())
    return {
        "id": f"ma-{chamber}-{d['EventId']}",
        "jurisdiction": "ma",
        "chamber": chamber,
        "committee": name or "Committee",
        "title": (d.get("Description") or name or "").strip(),
        "scheduled_iso": when or None,
        "scheduled_display": when,
        "timezone": "America/New_York",
        "location": location,
        "status": "canceled" if canceled else "scheduled",
        "bills": bills,
        "details_url": ma_hearing_page_url(d["EventId"]),
        # Written testimony is submitted on the hearing's own page (its "Submit
        # testimony" form), so that page is the participation link.
        "witness_slip_url": ma_hearing_page_url(d["EventId"]),
        "source": "malegislature.gov",
    }


def fetch_ma(begin, end):
    """Live: pull the hearing list, fetch a bounded prefix of details in parallel,
    and keep those in the [begin, end] window. The MA API has no date filter, so
    we bound client-side; fail-soft — a missed detail drops only that hearing."""
    listing = fetch_text(MA_LIST_URL)
    if not listing:
        return []
    ids = parse_ma_hearing_list(listing)[:MA_SCAN_LIMIT]
    if not ids:
        return []
    lo, hi = begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def one(eid):
        text = fetch_text(ma_detail_url(eid))
        return parse_ma_hearing(text) if text else None

    out = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rec in pool.map(one, ids):
            if not rec:
                continue
            day = (rec.get("scheduled_iso") or "")[:10]
            # Keep undated (can't prove past) and anything inside the window;
            # assemble() also drops past hearings, but bounding the far side here
            # keeps months-out hearings out of the near-future feed.
            if not day or lo <= day <= hi:
                out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Alaska — akleg.gov BASIS meetings JSON API
# --------------------------------------------------------------------------- #
# Alaska's BASIS service returns every committee meeting of a legislature as one
# keyless JSON document (committee, date/time, room, chamber, status). There is
# no bill agenda in the list, so Alaska hearings carry no bills — the reliable
# signal is the meeting itself plus the public-testimony (POM) link. The session
# number identifies the two-year legislature and must be bumped each biennium
# (34 = 2025-2026), the same maintenance step as Illinois's GA id.
AK_SESSION = "34"
AK_MEETINGS_URL = (f"https://www.akleg.gov/publicservice/basis/meetings"
                   f"?session={AK_SESSION}&json=true")
# BASIS asks callers to send a version header; it still serves data without one,
# but sending it avoids the deprecation warning.
AK_HEADERS = {"X-Alaska-Legislature-Basis-Version": "1.4"}


def _ak_title_case(name):
    """akleg publishes committee names in all caps ("LEGISLATIVE COUNCIL"); make
    them readable while leaving short all-cap tokens (<=3 chars, e.g. an acronym)
    alone."""
    def fix(w):
        return w if len(w) <= 3 and w.isalpha() else w.capitalize()
    return " ".join(fix(w) for w in (name or "").split())


def _ak_chamber(sponsor_type, chamber):
    if "joint" in (sponsor_type or "").lower():
        return "joint"
    return {"H": "house", "S": "senate"}.get((chamber or "").upper(), "other")


def ak_meeting_detail_url(raw_url):
    """The meeting's own akleg detail page. BASIS gives an http URL with spaces
    in the query (e.g. 'HRES 2026-09-10 13:00:00'); serve it as https and encode
    the spaces so the link works."""
    if not raw_url:
        return "https://www.akleg.gov/"
    return raw_url.replace("http://", "https://").replace(" ", "%20")


# akleg abbreviates the meeting room. Expand only the unambiguous ones so a
# reader isn't left decoding "ANCH LIO … Rm"; committee-room names (BARNES 124,
# SENATE FINANCE 532 — rooms in the Juneau Capitol) are already words and pass
# through untouched.
_AK_LOC_TERMS = [
    ("ANCH LIO", "Anchorage Legislative Information Office"),
    ("FBX LIO", "Fairbanks Legislative Information Office"),
    ("JNU LIO", "Juneau Legislative Information Office"),
    ("LIO", "Legislative Information Office"),
    ("Conf Rm", "Conference Room"),
    ("Rm", "Room"),
]


def _ak_location(raw):
    s = (raw or "").strip()
    for abbr, full in _AK_LOC_TERMS:
        s = re.sub(r"\b" + re.escape(abbr) + r"\b", full, s)
    return s


def parse_ak_meetings(json_text):
    """Pure: normalize the BASIS meetings document into hearing records within the
    current legislature. De-duplicates a joint committee that BASIS lists once per
    chamber (same sponsor/date/time), and skips rows with no date."""
    try:
        doc = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    meetings = ((doc or {}).get("Basis") or {}).get("Meetings") or []
    out, seen = [], set()
    for m in meetings if isinstance(meetings, list) else []:
        if not isinstance(m, dict):
            continue
        date = (m.get("MeetingDate") or "").strip()
        if not date:
            continue
        chamber = _ak_chamber(m.get("SponsorType"), m.get("Chamber"))
        sponsor = (m.get("MeetingSponsor") or "").strip()
        time = (m.get("MeetingTime") or "").strip()
        # A joint committee is published once per chamber — collapse to one.
        dedup_chamber = "joint" if chamber == "joint" else (m.get("Chamber") or "")
        key = (sponsor, date, time, dedup_chamber)
        if key in seen:
            continue
        seen.add(key)
        iso = f"{date}T{time}" if time else date
        out.append({
            "id": f"ak-{chamber}-{sponsor}-{date}-{time}".replace(":", "").replace(" ", ""),
            "jurisdiction": "ak",
            "chamber": chamber,
            "committee": _ak_title_case(m.get("MeetingTitle")) or "Committee",
            "title": (m.get("MeetingChangeText") or "").strip(),
            "scheduled_iso": iso or None,
            "scheduled_display": iso,
            "timezone": "America/Anchorage",
            "location": _ak_location(m.get("Location")),
            "status": "canceled" if m.get("MeetingCanceled") else "scheduled",
            "bills": [],
            "details_url": ak_meeting_detail_url(m.get("Url")),
            # Alaska's only comment tool is its general POMS form (no per-meeting
            # form), so the row's one link is the meeting's own page instead.
            "witness_slip_url": None,
            "source": "akleg.gov",
        })
    return out


def fetch_ak(begin, end):
    """Live: one request for the session's meetings, filtered to the window.
    Fail-soft: no data yields no Alaska hearings, never an error."""
    text = fetch_text_headers(AK_MEETINGS_URL, AK_HEADERS)
    if not text:
        return []
    lo, hi = begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    return [m for m in parse_ak_meetings(text)
            if not (m.get("scheduled_iso") or "")[:10]
            or lo <= m["scheduled_iso"][:10] <= hi]


# --------------------------------------------------------------------------- #
# Chicago City Council — council and committee meetings, from the City Clerk
# --------------------------------------------------------------------------- #
# The City Clerk's eLMS has a keyless, documented public API
# (api.chicityclerkelms.chicago.gov/swagger.json). /meeting-agenda lists every
# meeting; a committee meeting's "comment" carries its written public comment
# deadline (and, for a formal public hearing, its subject), and its "Notice"
# file says where to send written comments. One request per run.
CHI_API = "https://api.chicityclerkelms.chicago.gov/meeting-agenda"
CHI_MEETING_PAGE = "https://chicityclerkelms.chicago.gov/Meeting/?meetingId="


def chi_meetings_url(begin):
    q = {"filter": f"date gt {begin.strftime('%Y-%m-%dT00:00:00Z')}",
         "sort": "date asc", "top": "200"}
    return CHI_API + "?" + urllib.parse.urlencode(q)


def _chi_local(iso_utc):
    """'2026-11-30T16:30:00+00:00' -> naive Chicago wall-clock ISO, the shape the
    page and feeds expect (no offset applied)."""
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat((iso_utc or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%dT%H:%M:%S")


# "Written Public Comment Deadline: November 27, 2026, 10:30 AM" (the comma after
# the day is sometimes missing; "Written" sometimes is too).
_CHI_DEADLINE = re.compile(
    r"(?:Written\s+)?Public\s+Comment\s+Deadline:\s*([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}),?"
    r"\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\.?", re.IGNORECASE)


def chi_comment_deadline(text):
    """Pure: (deadline as naive Chicago ISO or None, text with that sentence removed)."""
    m = _CHI_DEADLINE.search(text or "")
    if not m:
        return None, (text or "").strip()
    day = re.sub(r",", "", m.group(1))
    clock = re.sub(r"[.\s]", "", m.group(2)).upper()
    try:
        iso = datetime.strptime(f"{day} {clock}", "%B %d %Y %I:%M%p").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None, (text or "").strip()
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    return iso, re.sub(r"\s+", " ", rest).strip()


def parse_chi_meetings(json_text):
    """Pure: eLMS /meeting-agenda JSON -> hearing records. Rows without an id or
    date are skipped; a canceled meeting is kept and flagged."""
    try:
        doc = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = (doc or {}).get("data") if isinstance(doc, dict) else None
    out = []
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, dict) or not m.get("meetingId"):
            continue
        local = _chi_local(m.get("date"))
        if not local:
            continue
        notice = next((f.get("path") for f in m.get("files") or []
                       if isinstance(f, dict) and f.get("attachmentType") == "Notice"
                       and str(f.get("path", "")).startswith("https://")), None)
        status = (m.get("status") or "").lower()
        deadline, subject = chi_comment_deadline(re.sub(r"\s+", " ", m.get("comment") or ""))
        rec = {
            "id": f"chi-{m['meetingId'].lower()}",
            "jurisdiction": "chi",
            "chamber": "other",
            "committee": (m.get("body") or "City Council").strip(),
            "title": subject,
            "scheduled_iso": local,
            "scheduled_display": local,
            "timezone": "America/Chicago",
            "location": re.sub(r"\s+", " ", m.get("location") or "").strip(),
            "status": "canceled" if "cancel" in status else "scheduled",
            "bills": [],
            "details_url": CHI_MEETING_PAGE + urllib.parse.quote(m["meetingId"]),
            # The meeting's Notice names the address for written comments.
            "witness_slip_url": notice,
            "source": "chicityclerkelms.chicago.gov",
        }
        if deadline:
            rec["comment_deadline"] = deadline
        out.append(rec)
    return out


# A committee's agenda is published as a PDF a few days before the meeting (the API's
# own agenda list is empty for almost every committee meeting). Committees lay their
# agendas out differently ("1. (O2026-0029491) Sale of ...", "[O2026-0028871]", tables
# of permit orders), but every item carries the Clerk's record number, so we take those,
# in agenda order, each once. Govbot's own Chicago record supplies each item's title and
# topics. The full City Council agenda runs to ~650 items, so only committee agendas are
# read, at most CHI_AGENDA_MAX items each.
CHI_AGENDA_MAX = 40
_CHI_RECORD = re.compile(r"\b(?:SO|SR|SOr|SA|SCL|O|R|Or|A|CL|F)20\d\d-\d{7}\b")


def parse_chi_agenda_text(text):
    """Pure: an agenda PDF's text -> bills [{id, url}] in agenda order, each once."""
    out, seen = [], set()
    for rid in _CHI_RECORD.findall(text or ""):
        if rid not in seen:
            seen.add(rid)
            out.append({"id": rid, "url": None})
            if len(out) >= CHI_AGENDA_MAX:
                break
    return out


def chi_agenda_pdfs(json_text):
    """Pure: eLMS /meeting-agenda JSON -> {hearing id: agenda PDF URL} for committee meetings."""
    try:
        rows = (json.loads(json_text) or {}).get("data") or []
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}
    out = {}
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, dict) or not m.get("meetingId") or (m.get("body") or "") == "City Council":
            continue
        pdf = next((f.get("path") for f in m.get("files") or [] if isinstance(f, dict)
                    and f.get("attachmentType") == "Agenda" and str(f.get("path", "")).startswith("https://")), None)
        if pdf:
            out[f"chi-{m['meetingId'].lower()}"] = pdf
    return out


def pdf_text(url):
    """Live: download a PDF and extract its text with pdftotext (poppler, a system tool,
    as Govbot's pipeline uses). Fail-soft: no tool or no file gives ''."""
    if not shutil.which("pdftotext"):
        return ""
    try:
        with _open(urllib.request.Request(url, headers={"User-Agent": UA}), FETCH_TIMEOUT) as r:
            data = r.read(20_000_000)
        return subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True,
                              timeout=60, check=False).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - fail-soft: a missing agenda just means no bills
        return ""


def fetch_chi(begin, end):
    """Live: one request for meetings after `begin`, kept to the window, then each
    posted committee agenda read for the items on it. Fail-soft: no data yields no
    Chicago meetings (or no bills), never an error."""
    text = fetch_text(chi_meetings_url(begin))
    if not text:
        return []
    hi = end.strftime("%Y-%m-%d")
    meetings = [m for m in parse_chi_meetings(text) if m["scheduled_iso"][:10] <= hi]
    pdfs = chi_agenda_pdfs(text)
    with ThreadPoolExecutor(max_workers=4) as pool:
        texts = dict(zip([m["id"] for m in meetings if m["id"] in pdfs],
                         pool.map(pdf_text, [pdfs[m["id"]] for m in meetings if m["id"] in pdfs])))
    for m in meetings:
        m["bills"] = parse_chi_agenda_text(texts.get(m["id"], ""))
    return meetings


# --------------------------------------------------------------------------- #
# USA (Federal) — rules open for public comment, from the Federal Register
# --------------------------------------------------------------------------- #
# Federal "hearings" are open public-comment periods on proposed (and some final)
# rules. The Federal Register's public API (federalregister.gov/developers) is
# keyless and lists every published document whose comment period is still open;
# most carry a Regulations.gov "comment on" link straight to that rule's comment
# form. One request per run. Fail-soft: no data yields no federal rows, never
# placeholders.
FR_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_FIELDS = ("title", "comments_close_on", "comment_url", "agency_names",
             "html_url", "document_number", "type")


RULE_TYPES = ("PRORULE", "RULE")
NOTICE_TYPES = ("NOTICE",)


def fr_open_for_comment_url(today, types):
    """Documents of `types` published by `today` (a date) whose comment period
    hasn't closed: proposed rules + rules, or notices (~800, fetched separately)."""
    q = [("per_page", "1000"), ("order", "newest"),
         ("conditions[comment_date][gte]", f"{today:%Y-%m-%d}"),
         ("conditions[publication_date][lte]", f"{today:%Y-%m-%d}")]
    q += [("conditions[type][]", t) for t in types]
    q += [("fields[]", f) for f in FR_FIELDS]
    return FR_API + "?" + urllib.parse.urlencode(q)


# Notices are announcements that ask for comments without being a rule. Each is put in
# one plain-language kind by its title (first match wins, so "…Information Collection…;
# Comment Request" is paperwork, not a request for ideas). Order = display order.
NOTICE_CATEGORIES = [
    ("rfi", "Requests for ideas & information",
     r"request for (information|comments?|input|public comment|nominations)|requests? for comment"
     r"|\bRFI\b|advance notice|petition for rulemaking|solicitation of (comments|input)"),
    ("environment", "Environment & wildlife reviews",
     r"environmental (impact|assessment)|\bNEPA\b|endangered|threatened species|habitat"
     r"|resource management plan|land use plan|wild(life|erness)"),
    ("permits", "Permits, licenses & approvals",
     r"permit|licens|application|approval|exemption|waiver|authoriz|certificat|registration|petition"),
    ("meetings", "Meetings & listening sessions",
     r"\bmeetings?\b|public hearing|listening session|webinar|workshop"),
    ("privacy", "Privacy & government records", r"privacy act|systems? of records"),
    ("paperwork", "Forms & paperwork reviews",
     r"information collection|paperwork reduction|submission for (omb|office of management)"
     r"|omb review|proposed (data )?collection|previously approved collection"),
]
NOTICE_OTHER = ("other", "Other notices")
# Checked in this order (paperwork first: its titles often also say "comment request").
_NOTICE_MATCH_ORDER = ["paperwork", "privacy", "rfi", "environment", "meetings", "permits"]


def fr_notice_category(title):
    pats = {k: p for k, _, p in NOTICE_CATEGORIES}
    for key in _NOTICE_MATCH_ORDER:
        if re.search(pats[key], title or "", re.I):
            return key
    return NOTICE_OTHER[0]


def _fed_pretty_date(iso):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return ""
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]
    return f"{months[int(m.group(2)) - 1]} {int(m.group(3))}, {m.group(1)}"


def parse_fr_documents(json_text):
    """Pure: Federal Register documents JSON -> hearing records, soonest-closing
    first. The one link is the rule's Regulations.gov comment form when it has one,
    else its Federal Register page (which says how to comment by mail/email)."""
    try:
        docs = (json.loads(json_text) or {}).get("results") or []
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []
    out = []
    for d in docs if isinstance(docs, list) else []:
        num, close = d.get("document_number"), d.get("comments_close_on")
        page = d.get("html_url")
        if not (num and close and page):
            continue
        comment = (d.get("comment_url") or "").replace("http://", "https://", 1)
        # A Regulations.gov comment link names the rule's docket document
        # (e.g. EPA-HQ-OAR-2026-0123-0001); that id is what people search for.
        doc_id = comment.rsplit("/", 1)[-1] if "/commenton/" in comment else num
        agencies = d.get("agency_names") or []
        title = (d.get("title") or "").strip()
        rec = {
            "id": f"us-{num}",
            "jurisdiction": "us",
            "chamber": "federal",
            "committee": agencies[-1] if agencies else "Federal agency",  # most specific
            "title": title,
            "scheduled_iso": close,
            "scheduled_display": "Comments due " + _fed_pretty_date(close),
            "timezone": None,
            "location": "Online · " + ("Regulations.gov" if comment else "Federal Register"),
            "status": "open",
            "bills": [{"id": doc_id, "title": title, "url": page, "slips": None}],
            "details_url": page,
            "witness_slip_url": comment or page,
            "source": "federalregister.gov",
        }
        if d.get("type") == "Notice":
            rec["category"] = fr_notice_category(title)
        out.append(rec)
    out.sort(key=lambda h: (h["scheduled_iso"], h["id"]))
    return out


# The page shows the SHOW_MAX soonest-closing rules and the NOTICE_SHOW soonest-closing
# notices of each kind, each list with a "See all" link to the Federal Register's own list.
SHOW_MAX = 7
NOTICE_SHOW = 2

def fr_full_list_url(today, types=RULE_TYPES):
    """The Federal Register's own search page for documents of `types` whose comment
    period is still open (its HTML search takes the API's conditions, with the date
    as MM/DD/YYYY)."""
    q = [("conditions[comment_date][gte]", f"{today:%m/%d/%Y}")]
    q += [("conditions[type][]", t) for t in types]
    return "https://www.federalregister.gov/documents/search?" + urllib.parse.urlencode(q)


def federal_preview(recs, today):
    """Pure: keep the SHOW_MAX soonest-closing open rules and NOTICE_SHOW notices of each
    kind, and describe the full lists for the page's "See all" links."""
    order = lambda h: (h["scheduled_iso"] or "9999", h["id"])
    rules = sorted((h for h in recs if not h.get("category")), key=order)
    notices = sorted((h for h in recs if h.get("category")), key=order)
    shown = rules[:SHOW_MAX]
    meta = {"open_total": len(rules), "full_list_url": fr_full_list_url(today)}
    if notices:
        cats = []
        for key, label in [(k, l) for k, l, _ in NOTICE_CATEGORIES] + [NOTICE_OTHER]:
            these = [h for h in notices if h["category"] == key]
            if these:
                shown += these[:NOTICE_SHOW]
                cats.append({"key": key, "label": label, "total": len(these)})
        meta.update({"notice_total": len(notices), "notice_categories": cats,
                     "notice_list_url": fr_full_list_url(today, NOTICE_TYPES)})
    return shown, meta


def fetch_fr(url, max_pages=3):
    """All pages of one Federal Register query (the API pages at 1,000)."""
    docs = []
    for _ in range(max_pages):
        text = fetch_text(url) if url else None
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            break
        docs += data.get("results") or []
        url = data.get("next_page_url")
        if not url:
            break
    return docs


def fetch_us(today):
    docs = (fetch_fr(fr_open_for_comment_url(today, RULE_TYPES))
            + fetch_fr(fr_open_for_comment_url(today, NOTICE_TYPES)))
    return parse_fr_documents(json.dumps({"results": docs}))


# --------------------------------------------------------------------------- #
# Iowa public hearings (legis.iowa.gov's own Public Hearings list)
# --------------------------------------------------------------------------- #
# Iowa holds formal public hearings on particular bills, each with its own notice, a sign-up /
# comment form while it's open, the comments people sent, and video. They are listed on the
# Legislature's Public Hearings page (one table, one row per hearing); Iowa's ordinary committee
# meetings come from Open States' scraper as usual, so both show.
IA_HEARINGS = "https://www.legis.iowa.gov/committees/publicHearings"
IA_BASE = "https://www.legis.iowa.gov"


def parse_ia_public_hearings(html_text):
    """The Public Hearings table -> hearing records. Pure."""
    out = []
    # (the page never closes its <tbody>, so read to the end of the table)
    body = re.search(r'<table[^>]*id="sortableTable".*?<tbody>(.*?)</table>', html_text or "", re.S)
    for row in re.findall(r"<tr>(.*?)</tr>", body.group(1) if body else "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 6:
            continue
        m = re.match(r"\s*(\d\d)/(\d\d)/(\d{4})\s+(\d{1,2}):(\d\d)\s*([AP]M)", _plain(cells[0]), re.I)
        mid = re.search(r"meetingID=(\d+)", row)
        if not (m and mid):
            continue
        mo, d, y, hh, mm, ap = m.groups()
        hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
        iso = f"{y}-{mo}-{d}T{hh:02d}:{mm}:00"
        href = lambda cell: next(iter(re.findall(r'href="([^"]+)"', cell)), None)
        absu = lambda u: urllib.parse.urljoin(IA_HEARINGS, html.unescape(u)) if u else None
        bills = []
        for ba in re.findall(r"BillBook\?ga=(\d+)&(?:amp;)?ba=([A-Z]+ ?\d+)", cells[2]):
            bills.append({"id": ba[1].replace(" ", ""), "slips": None,
                          "url": f"{IA_BASE}/legislation/BillBook?ga={ba[0]}&ba={urllib.parse.quote(ba[1])}"})
        subject = _plain(cells[3]) or _plain(cells[2])
        first = bills[0]["id"] if bills else ""
        signup = absu(href(cells[5]))               # only while sign-up is open
        out.append({
            "id": f"ia-hearing-{mid.group(1)}", "jurisdiction": "ia",
            "chamber": "senate" if first.startswith("S") else "house" if first.startswith("H") else "other",
            "committee": "Iowa Legislature public hearing", "title": subject,
            "scheduled_iso": iso, "scheduled_display": iso, "timezone": "America/Chicago",
            "location": (_plain(cells[1]) + " · " if _plain(cells[1]) else "") + "Iowa State Capitol, Des Moines",
            "status": "canceled" if re.search(r"cancel", subject, re.I) else "scheduled",
            "bills": bills,
            "details_url": absu(href(cells[4])) or IA_HEARINGS,   # the hearing's own notice
            "witness_slip_url": signup, "source": "legis.iowa.gov",
        })
    return out


def fetch_ia_hearings():
    return parse_ia_public_hearings(fetch_text(IA_HEARINGS) or "")


# --------------------------------------------------------------------------- #
# U.S. Congress committee hearings (code "usa", Govbot's code for federal bills)
# --------------------------------------------------------------------------- #
# Read straight from each chamber's own published schedule, no key and no API:
# * Senate: senate.gov's committee schedule file (hearings.xml), one request.
# * House: the Clerk's committee repository (docs.house.gov). Its week calendar lists
#   each meeting; each meeting page's "Download Meeting XML" button (an ASP.NET form that
#   posts back to the page) returns that meeting as public-domain XML. We press it.
SENATE_XML = "https://www.senate.gov/general/committee_schedules/hearings.xml"
SENATE_SCHEDULE = "https://www.senate.gov/committees/hearings_meetings.htm"   # the Senate's schedule page
HOUSE_WEEK = "https://docs.house.gov/Committee/Calendar/ByWeek.aspx?WeekOf={}"
HOUSE_EVENT = "https://docs.house.gov/Committee/Calendar/ByEvent.aspx?EventID={}"
HOUSE_WEEKS = 4                  # committees post notices about a week ahead
HOUSE_MAX = 200                  # meetings read per run, at most
CONGRESS_TZ = "America/New_York"
HOUSE_BUILDINGS = {"CAPITOL": "U.S. Capitol", "CVC": "Capitol Visitor Center",
                   "RHOB": "Rayburn House Office Building", "LHOB": "Longworth House Office Building",
                   "CHOB": "Cannon House Office Building", "FHOB": "Ford House Office Building",
                   "OHOB": "O'Neill House Office Building"}
# Congress.gov's own page for a bill, by the bill's type.
_CG_TYPE = {"HR": "house-bill", "S": "senate-bill", "HJRES": "house-joint-resolution",
            "SJRES": "senate-joint-resolution", "HCONRES": "house-concurrent-resolution",
            "SCONRES": "senate-concurrent-resolution", "HRES": "house-resolution",
            "SRES": "senate-resolution"}
_CONGRESS_BILL_RE = re.compile(
    r"\b(H\.\s?R\.|S\.|H\.\s?J\.\s?Res\.|S\.\s?J\.\s?Res\.|H\.\s?Con\.\s?Res\.|"
    r"S\.\s?Con\.\s?Res\.|H\.\s?Res\.|S\.\s?Res\.)\s?(\d{1,5})\b", re.I)


def congress_number(day):
    """'2026-12-01' -> 119 (a new Congress starts January 3 of each odd year)."""
    y, m, d = (int(x) for x in day[:10].split("-"))
    if m == 1 and d < 3:
        y -= 1
    return (y - 1789) // 2 + 1


def congress_bill(kind, num, congress):
    """('H.R.', '2347', 119) -> {"id": "HR2347", "url": congress.gov page}."""
    k = re.sub(r"[\s.]", "", kind).upper()
    return {"id": f"{k}{int(num)}", "slips": None,
            "url": f"https://www.congress.gov/bill/{congress}th-congress/{_CG_TYPE[k]}/{int(num)}"
            if k in _CG_TYPE else None}


def congress_bills(text, congress):
    out, seen = [], set()
    for kind, num in _CONGRESS_BILL_RE.findall(text or ""):
        b = congress_bill(kind, num, congress)
        if b["id"] not in seen:
            seen.add(b["id"])
            out.append(b)
    return out


def parse_senate_hearings(xml_text):
    """senate.gov hearings.xml -> hearing records. Pure."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for m in root.findall("meeting"):
        g = lambda tag: re.sub(r"\s+", " ", m.findtext(tag) or "").strip()
        ident, day = g("identifier"), g("date_iso_8601")
        if not ident or not day or not g("committee"):
            continue                        # "No committee hearings scheduled" rows
        time = g("time_iso_8601") or "00:00:00"
        committee = "Senate " + g("committee") + (f" · {g('sub_cmte')}" if g("sub_cmte") else "")
        matter = g("matter")
        kind = g("type")
        congress = congress_number(day)
        bills = []
        for doc in m.findall("Documents/AssociatedDocument"):
            pre = re.sub(r"[\s.]", "", doc.get("document_prefix") or "").upper()
            if pre in _CG_TYPE and (doc.get("document_num") or "").strip().isdigit():
                b = congress_bill(pre, doc.get("document_num").strip(), int(doc.get("congress") or congress))
                if b["id"] not in {x["id"] for x in bills}:
                    bills.append(b)
        bills += [b for b in congress_bills(matter, congress) if b["id"] not in {x["id"] for x in bills}]
        video = g("video_url")
        out.append({
            "id": f"usa-senate-{ident}", "jurisdiction": "usa", "chamber": "senate",
            "committee": committee,
            "title": (f"{kind}: " if kind else "") + matter,
            "scheduled_iso": f"{day}T{time[:8]}", "scheduled_display": f"{day}T{time[:8]}",
            "timezone": CONGRESS_TZ,
            "location": (g("room") + " · " if g("room") else "") + "U.S. Capitol complex, Washington, DC",
            "status": "canceled" if re.search(r"\b(cancel|postpone)", matter + kind, re.I) else "scheduled",
            "bills": bills, "witness_slip_url": None, "source": "senate.gov",
        })
        # The hearing's own page on senate.gov is its video page; without one, no link
        # (the Senate's schedule has no other page per hearing).
        if video.startswith("https://www.senate.gov/"):
            out[-1]["details_url"] = video
        else:
            out[-1]["calendar_url"] = SENATE_SCHEDULE
    return out


def parse_house_week(page_html):
    """A docs.house.gov week calendar page -> its meetings' EventIDs, in order. Pure."""
    seen, out = set(), []
    for eid in re.findall(r"ByEvent\.aspx\?EventID=(\d+)", page_html or ""):
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def parse_house_meeting(xml_text, event_id):
    """One House meeting's XML (the "Download Meeting XML" file) -> a hearing record. Pure."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    det = root.find("meeting-details")
    if det is None:
        return None
    g = lambda path: re.sub(r"\s+", " ", det.findtext(path) or "").strip()
    day = g("meeting-date/calendar-date")
    if not day:
        return None
    time = g("meeting-date/start-time") or "00:00:00"
    committees = [re.sub(r"\s+", " ", c.text or "").strip()
                  for c in det.findall("committees/committee-name") if (c.text or "").strip()]
    subs = [c for c in det.findall("subcommittees/committee-name") if (c.text or "").strip()]
    if not committees and subs:              # a subcommittee meeting names its parent
        committees = [subs[0].get("parent-name") or ""]
    committee = "House " + (committees[0] or "committee") + \
        (" · " + re.sub(r"\s+", " ", subs[0].text).strip() if subs else "")
    room, building = g("meeting-location/capitol-complex/room"), g("meeting-location/capitol-complex/building")
    building = HOUSE_BUILDINGS.get(building.upper(), building.title()) if building != "Select one" else ""
    where = " · ".join(x for x in (room, building) if x) or g("meeting-location/other-location") or ""
    title = g("meeting-title")
    status = (root.findtext("current-status") or "").strip().upper()
    congress = int(root.get("congress-num") or congress_number(day))
    bills, seen = [], set()
    # Bills on the agenda: the "BR" documents link each bill's text on govinfo.gov
    # (BILLS-119hr2347eas -> HR2347); the title catches any the documents don't.
    for doc in root.findall("meeting-documents/meeting-document"):
        for f in doc.findall("files/file"):
            m = re.search(r"BILLS-(\d+)(hr|s|hjres|sjres|hconres|sconres|hres|sres)(\d+)", f.get("doc-url") or "")
            if m:
                b = congress_bill(m.group(2), m.group(3), int(m.group(1)))
                if b["id"] not in seen:
                    seen.add(b["id"])
                    bills.append(b)
    for b in congress_bills(title, congress):
        if b["id"] not in seen:
            seen.add(b["id"])
            bills.append(b)
    if status in ("P",):
        title = "Postponed: " + title
    return {
        "id": f"usa-house-{event_id}", "jurisdiction": "usa", "chamber": "house",
        "committee": committee, "title": title,
        "scheduled_iso": f"{day}T{time[:8]}", "scheduled_display": f"{day}T{time[:8]}",
        "timezone": CONGRESS_TZ,
        "location": (where + " · " if where else "") + "Washington, DC",
        "status": "canceled" if status in ("C", "P", "X") else "scheduled",
        "bills": bills, "details_url": HOUSE_EVENT.format(event_id),
        "witness_slip_url": None, "source": "docs.house.gov",
    }


def _house_meeting_xml(event_id):
    """Press the meeting page's "Download Meeting XML" button: GET the page, then post its
    form back with that button as the target. None on any failure."""
    import html as H
    import http.cookiejar
    url = HOUSE_EVENT.format(event_id)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", UA)]
    try:
        page = op.open(url, timeout=FETCH_TIMEOUT).read().decode("utf-8", "replace")
        form = {k: H.unescape(v) for k, v in re.findall(
            r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page)}
        if "__VIEWSTATE" not in form:
            return None
        form.update(__EVENTTARGET="ctl00$MainContent$LinkButtonDownloadMtgXML", __EVENTARGUMENT="")
        with op.open(url, data=urllib.parse.urlencode(form).encode(), timeout=FETCH_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
        return body if "<committee-meeting" in body else None
    except Exception as err:  # network, TLS, timeout: that meeting is skipped
        print(f"warning: House meeting {event_id}: {err}", file=sys.stderr)
        return None


def fetch_usa(now):
    """Congress: the Senate's schedule + the House's next HOUSE_WEEKS weeks. Fail-soft."""
    hearings = parse_senate_hearings(fetch_text(SENATE_XML) or "")
    sunday = (now - timedelta(days=(now.weekday() + 1) % 7)).date()
    ids = []
    for w in range(HOUSE_WEEKS):
        a = sunday + timedelta(days=7 * w)
        b = a + timedelta(days=6)
        week = f"{a:%m%d%Y}_{b:%m%d%Y}"
        for eid in parse_house_week(fetch_text(HOUSE_WEEK.format(week)) or ""):
            if eid not in ids:
                ids.append(eid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        xmls = list(pool.map(_house_meeting_xml, ids[:HOUSE_MAX]))
    for eid, xml in zip(ids, xmls):
        rec = parse_house_meeting(xml, eid) if xml else None
        if rec:
            hearings.append(rec)
    print(f"Congress: {len(hearings)} meetings ({len(ids)} House on the calendar)", file=sys.stderr)
    return hearings


# --------------------------------------------------------------------------- #
# Every other state: Open States' open-source meeting scrapers, run by us
# --------------------------------------------------------------------------- #
# The refresh workflow runs Open States' "events" scrapers (github.com/openstates/
# openstates-scrapers, the project Govbot's bill data comes from) in their Docker image.
# Each one reads its legislature's own website (committee calendars, agenda pages,
# PDFs), so nothing here depends on anyone's API staying up: it's open-source code we
# run ourselves. They write one Open Civic Data event per meeting to
# <dir>/<code>/event_*.json, and the workflow drops an "_ok" file beside them when the
# scraper finished. These functions only read those files (standard library only).

# Each place's legislature meets at its capital; the scrapers give UTC times.
STATE_TZ = {
    "al": "America/Chicago", "ak": "America/Anchorage", "az": "America/Phoenix",
    "ar": "America/Chicago", "ca": "America/Los_Angeles", "co": "America/Denver",
    "ct": "America/New_York", "de": "America/New_York", "dc": "America/New_York",
    "fl": "America/New_York", "ga": "America/New_York", "hi": "Pacific/Honolulu",
    "id": "America/Boise", "il": "America/Chicago", "in": "America/Indiana/Indianapolis",
    "ia": "America/Chicago", "ks": "America/Chicago", "ky": "America/New_York",
    "la": "America/Chicago", "me": "America/New_York", "md": "America/New_York",
    "ma": "America/New_York", "mi": "America/Detroit", "mn": "America/Chicago",
    "ms": "America/Chicago", "mo": "America/Chicago", "mt": "America/Denver",
    "ne": "America/Chicago", "nv": "America/Los_Angeles", "nh": "America/New_York",
    "nj": "America/New_York", "nm": "America/Denver", "ny": "America/New_York",
    "nc": "America/New_York", "nd": "America/Chicago", "oh": "America/New_York",
    "ok": "America/Chicago", "or": "America/Los_Angeles", "pa": "America/New_York",
    "ri": "America/New_York", "sc": "America/New_York", "sd": "America/Chicago",
    "tn": "America/Chicago", "tx": "America/Chicago", "ut": "America/Denver",
    "vt": "America/New_York", "va": "America/New_York", "wa": "America/Los_Angeles",
    "wv": "America/New_York", "wi": "America/Chicago", "wy": "America/Denver",
    "gu": "Pacific/Guam", "mp": "Pacific/Saipan", "vi": "America/St_Thomas",
    "pr": "America/Puerto_Rico", "as": "Pacific/Pago_Pago", "chi": "America/Chicago",
}
OS_TITLE_MAX = 600
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _os_chamber(name):
    """House / Senate / joint from a committee name; "other" when it doesn't say."""
    n = (name or "").lower()
    if re.search(r"\b(joint|conference|interim)\b", n):
        return "joint"
    if re.search(r"\bsenate\b", n):
        return "senate"
    if re.search(r"\b(house|assembly|delegates)\b", n):
        return "house"
    return "other"


def _os_bill_id(name):
    """'HB 1234' / 'S.B. 12' / 'HJR 3' -> 'HB1234' / 'SB12' / 'HJR3'; None if it isn't one."""
    s = re.sub(r"[\s.\-]", "", name or "").upper()
    return s if re.fullmatch(r"[A-Z]{1,6}\d{1,6}[A-Z]?", s) else None


# Every hearing links straight to its own official page (or its own agenda / notice), never
# a calendar, schedule or list of meetings. A link counts as the hearing's own when it isn't
# shared with another meeting of the same place and isn't a calendar-type page without a
# meeting id. With nothing better, a row gets no link rather than a calendar.
_GENERIC_LINK = re.compile(
    r"schedule|sched\.|calendar|alis-today|upcominghearings|downloads\.asp|weeklyview|"
    r"meetingslist|meetings\.php|/committees?/?(#|$)|search|committee_bio", re.I)
_MEETING_ID = re.compile(r"[?&](id|mtgid|event|eventid|meeting|meetingid|agendaid|key|m)=[^&]*\d", re.I)


def specific_link(url, shared=()):
    """True when url can be one hearing's own page: not shared, not a calendar/list page."""
    if not url or url in shared:
        return False
    return not _GENERIC_LINK.search(url) or bool(_MEETING_ID.search(url))


def _os_link_candidates(ev):
    """An event's links, best first: its own pages, then its agenda / notice, then its
    video or audio. Relative links are resolved against its first source."""
    base = next((x.get("url") for x in ev.get("sources") or [] if x.get("url")), "")
    fix = lambda u: urllib.parse.urljoin(base, u) if u and not u.startswith("http") else u
    pages = [fix(x.get("url")) for x in (ev.get("sources") or []) + (ev.get("links") or [])]
    docs = [fix(l.get("url")) for d in ev.get("documents") or []
            if re.search(r"agenda|notice|hearing", (d.get("note") or "") + " " +
                         " ".join(l.get("url") or "" for l in d.get("links") or []), re.I)
            for l in d.get("links") or []]
    media = [fix(l.get("url")) for m in ev.get("media") or [] for l in m.get("links") or []]
    return [u for u in pages + docs + media if (u or "").startswith(("http://", "https://"))]


def _os_junk_name(name):
    """A scraper that misread its source: a "meeting" named after a time of day or a
    table header (Mississippi's agenda PDF, Nov 2026). Such rows are dropped, never shown."""
    return bool(re.fullmatch(r"\d{1,2}(:\d\d)?\s*[ap]\.?m\.?", name, re.I)) \
        or name.lower() in ("presenter / organization",)


def _plain(text):
    """Source text as plain words: tags dropped, entities (&#58;) decoded, spaces collapsed.
    (The page still sets it with textContent; this is for readability, not safety.)"""
    import html
    lines = [html.unescape(re.sub(r"<[^>]+>", " ", x)).strip() for x in (text or "").splitlines()]
    # A line that is only a link's label ("(Agenda)", "(View Meeting)") lost its link here.
    lines = [x for x in lines if x and not re.fullmatch(r"(\([^)]*\)\s*)+", x)]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def parse_os_event(json_text, code):
    """One Open States event file -> a hearing record (schema shape), or None.
    Pure: no network, no clock."""
    try:
        ev = json.loads(json_text)
    except ValueError:
        return None
    name = _plain(ev.get("name"))
    start = ev.get("start_date") or ""
    if not name or not start or _os_junk_name(name):
        return None
    tz = STATE_TZ.get(code, "America/New_York")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):          # a date with no time posted
        y, mo, d = (int(x) for x in start.split("-"))
        iso = f"{start}T00:00:00"
        display = f"{_MONTHS[mo - 1]} {d}, {y} · time not posted"
    else:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tz))
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
        display = iso
        if iso.endswith("T00:00:00"):         # local midnight = the source gave no time (NJ)
            display = f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year} · time not posted"
    host = next((p.get("name") for p in ev.get("participants") or []
                 if p.get("note") == "host" and p.get("name")), None)
    committee = host or name
    urls = [s.get("url") for s in (ev.get("sources") or []) + (ev.get("links") or [])
            if (s.get("url") or "").startswith(("http://", "https://"))]
    details = urls[0] if urls else None          # only for the id (kept as it always was)
    cands = _os_link_candidates(ev)
    bills, seen = [], set()
    for item in ev.get("agenda") or []:
        for ent in item.get("related_entities") or []:
            bid = _os_bill_id(ent.get("name")) if ent.get("entity_type") == "bill" else None
            if bid and bid not in seen:
                seen.add(bid)
                bills.append({"id": bid, "url": None, "slips": None})
    desc = _plain(ev.get("description"))
    title = desc or (name if name != committee else "")
    # "Meeting of the Assembly Health Committee" says nothing the committee name doesn't.
    if committee.lower() in title.lower() and len(title) < len(committee) + 25:
        title = ""
    if len(title) > OS_TITLE_MAX:
        title = title[:OS_TITLE_MAX - 1].rstrip() + "…"
    # A stable id: the scraper's own upstream id when it has one, else the meeting's
    # official page + name + start (Open States' "_id" is new on every run).
    key = ev.get("upstream_id") or (ev.get("extras") or {}).get("agendaId") \
        or f"{details}|{name}|{start}"
    import hashlib
    hid = f"{code}-os-{hashlib.sha1(str(key).encode()).hexdigest()[:12]}"
    loc = _plain((ev.get("location") or {}).get("name"))
    if title == loc:                              # Virginia repeats the place as its description
        title = ""
    rec = {
        "id": hid, "jurisdiction": code, "chamber": _os_chamber(committee),
        "committee": committee, "title": title,
        "scheduled_iso": iso, "scheduled_display": display, "timezone": tz,
        "location": "" if loc.upper() in ("TBD", "TBA") else loc,
        "status": "canceled" if (ev.get("status") or "").startswith("cancel") else "scheduled",
        "bills": bills, "witness_slip_url": None,
        "source": urllib.parse.urlparse(details).hostname if details else "openstates-scrapers",
        "_links": cands,
    }
    return rec


def _date_tokens(iso):
    """'2026-12-01T…' -> ways a file name writes that day: 12-1-26, 12-01-2026, 20261201, …"""
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    return {f"{m}-{d}-{y % 100:02d}", f"{m:02d}-{d:02d}-{y}", f"{m}-{d}-{y}", f"{y}-{m:02d}-{d:02d}",
            f"{m:02d}{d:02d}{y}", f"{y}{m:02d}{d:02d}", f"{m}/{d}/{y}"}


def _set_os_link(rec, shared=()):
    """Set details_url to the first link that is this hearing's own. A link other meetings
    also list still counts when its name carries this meeting's date (Kentucky lists every
    agenda of a committee under each meeting: "12-1-26 Agenda.pdf")."""
    days = _date_tokens(rec["scheduled_iso"]) if rec.get("scheduled_iso") else set()
    dated = lambda u: not _GENERIC_LINK.search(u) and any(t in urllib.parse.unquote(u) for t in days)
    links = rec.pop("_links", [])
    own = next((u for u in links if specific_link(u, shared) or dated(u)), None)
    if own:
        rec["details_url"] = own
    elif links:
        # No page of its own: keep the official listing it came from, shown as its source.
        rec["calendar_url"] = links[0]


def load_os_events(os_dir, codes):
    """Read <os_dir>/<code>/event_*.json for each code. Returns (hearings, ok_codes):
    ok_codes are the places whose scraper finished (an "_ok" file), so an empty list
    there means "no meetings posted", not "the scraper broke"."""
    hearings, ok = [], set()
    root = Path(os_dir)
    for code in codes:
        d = root / code
        if not d.is_dir():
            continue
        if (d / "_ok").exists():
            ok.add(code)
        seen, recs = set(), []
        for f in sorted(d.glob("event_*.json")):
            rec = parse_os_event(f.read_text(errors="replace"), code)
            if rec and rec["id"] not in seen:     # one meeting scraped twice is one meeting
                seen.add(rec["id"])
                recs.append(rec)
        # A link more than one meeting uses is a list or calendar page, not a hearing's own.
        uses = {}
        for r in recs:
            for u in set(r["_links"]):
                uses[u] = uses.get(u, 0) + 1
        shared = {u for u, n in uses.items() if n > 1}
        for r in recs:
            _set_os_link(r, shared)
        hearings += recs
    return hearings, ok


def participation_jurisdictions(path):
    """participation.json -> {code: jurisdiction entry}, for places with no entry in
    JURISDICTIONS (every state the Open States scrapers cover)."""
    try:
        rows = json.loads(Path(path).read_text()).get("jurisdictions", [])
    except (OSError, ValueError):
        return {}
    return {r["code"]: {k: r[k] for k in ("code", "name", "participation", "portal_url")}
            for r in rows if r.get("code") and r.get("portal_url")}


# --------------------------------------------------------------------------- #
# data/: every hearing kept in git, like Govbot's bill repos
# --------------------------------------------------------------------------- #
# data/<code>/<id>.json holds one meeting each (minus Govbot's own title/topic
# enrichment, which is Govbot's data, not the source's). Past meetings stay forever, so
# the folder is a history anyone can clone; a run only rewrites a file whose content
# changed. Upcoming meetings that a place's scraper no longer lists (moved or dropped)
# are removed, but only after a run that finished for that place. When a place's
# scraper breaks, its upcoming meetings come from here instead, so an outage never
# empties a state. data/_status.json records each place's last good run.
ARCHIVE_SKIP = {"us"}
# Places our own scrapers cover in full, so Open States' meetings for them are ignored. (Iowa is
# not here: our scraper reads only its public hearings, Open States its committee meetings.)
OS_SKIP = {"il", "wa", "ma", "ak", "chi", "us", "usa"}       # federal comment periods are archived by the Federal Register


def _archive_record(h):
    rec = dict(h)
    rec["bills"] = [{k: v for k, v in b.items() if not k.startswith("govbot_")}
                    for b in h.get("bills", [])]
    return rec


def _dump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def archive_hearings(hearings, data_dir, ok_codes, now):
    """Write this run's hearings into data_dir; drop upcoming meetings a finished
    scraper no longer lists. Returns the number of files written."""
    root = Path(data_dir)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_code = {}
    for h in hearings:
        if h["jurisdiction"] not in ARCHIVE_SKIP:
            by_code.setdefault(h["jurisdiction"], []).append(h)
    written = 0
    for code in set(by_code) | set(ok_codes):
        if code in ARCHIVE_SKIP:
            continue
        d = root / code
        d.mkdir(parents=True, exist_ok=True)
        current = {h["id"] for h in by_code.get(code, [])}
        if code in ok_codes:
            for f in d.glob("*.json"):
                if f.stem in current or "-os-" not in f.stem:   # only prune what Open States listed
                    continue
                try:
                    old = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if (old.get("scheduled_iso") or "9999")[:10] >= today:
                    f.unlink()
        for h in by_code.get(code, []):
            f = d / f"{h['id']}.json"
            rec = _archive_record(h)
            try:
                rec["first_seen"] = json.loads(f.read_text()).get("first_seen", stamp)
            except (OSError, ValueError):
                rec["first_seen"] = stamp
            text = _dump(rec)
            if not f.exists() or f.read_text() != text:
                f.write_text(text)
                written += 1
    return written


def restore_from_archive(data_dir, codes, now):
    """Upcoming meetings stored in data_dir for these places (their scraper broke)."""
    today = now.strftime("%Y-%m-%d")
    out = []
    for code in codes:
        for f in sorted((Path(data_dir) / code).glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if (rec.get("scheduled_iso") or "")[:10] >= today:
                rec.pop("first_seen", None)
                out.append(rec)
    return out


def update_status(data_dir, attempted, ok_codes, hearings, now):
    """data/_status.json: per place, the last run that finished, how many meetings it
    found, and how many runs in a row have failed since. Returns codes failing 3+ runs."""
    p = Path(data_dir) / "_status.json"
    try:
        status = json.loads(p.read_text())
    except (OSError, ValueError):
        status = {}
    counts = {}
    for h in hearings:
        counts[h["jurisdiction"]] = counts.get(h["jurisdiction"], 0) + 1
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for code in attempted:
        s = status.setdefault(code, {})
        if code in ok_codes:
            s.update(last_ok=stamp, meetings=counts.get(code, 0), failed_runs=0)
        else:
            s["failed_runs"] = s.get("failed_runs", 0) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_dump(dict(sorted(status.items()))))
    return sorted(c for c in attempted if status[c].get("failed_runs", 0) >= 3)


# --------------------------------------------------------------------------- #
# Downloads: CSV for spreadsheets, .ics for calendars (site/downloads/)
# --------------------------------------------------------------------------- #
CSV_COLUMNS = ["jurisdiction", "place", "date", "time", "timezone", "committee", "chamber",
               "title", "location", "status", "bills", "details_url", "first_seen", "id"]


def _csv_cell(v):
    """A cell starting with = + - @ gets a leading ' so a spreadsheet can't run it."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def hearings_csv(rows, names):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        iso = r.get("scheduled_iso") or ""
        untimed = "time not posted" in (r.get("scheduled_display") or "")
        w.writerow([_csv_cell(x) for x in (
            r["jurisdiction"], names.get(r["jurisdiction"], r["jurisdiction"]),
            iso[:10], "" if untimed else iso[11:16], r.get("timezone"),
            r.get("committee"), r.get("chamber"), r.get("title"), r.get("location"),
            r.get("status"), "; ".join(b["id"] for b in r.get("bills", [])),
            r.get("details_url"), r.get("first_seen"), r["id"])])
    return "﻿" + buf.getvalue()


def _ics_text(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,") \
        .replace("\r", "").replace("\n", "\\n")


def _ics_fold(line):
    """Fold a content line at 75 octets (RFC 5545 3.1)."""
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75 if not out else 74
        while cut and (b[cut] & 0xC0) == 0x80:      # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n ".join(out)


def hearings_ics(rows, cal_name):
    """An iCalendar file of upcoming meetings (times in UTC, so no VTIMEZONE needed).
    No end time is given: the sources rarely publish one, and we don't invent it."""
    from zoneinfo import ZoneInfo
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Govbot//Public Hearings//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:" + _ics_text(cal_name),
             "X-PUBLISHED-TTL:PT12H", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    for r in rows:
        iso = r.get("scheduled_iso")
        if not iso:
            continue
        stamp = (r.get("first_seen") or "1970-01-01T00:00:00Z").replace("-", "").replace(":", "")
        lines += ["BEGIN:VEVENT", f"UID:{r['id']}@govbot-public-hearings", f"DTSTAMP:{stamp}"]
        if "time not posted" in (r.get("scheduled_display") or ""):
            lines.append("DTSTART;VALUE=DATE:" + iso[:10].replace("-", ""))
        else:
            dt = datetime.fromisoformat(iso).replace(
                tzinfo=ZoneInfo(r.get("timezone") or "America/Chicago"))
            lines.append("DTSTART:" + dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        bills = ", ".join(b["id"] for b in r.get("bills", []))
        desc = "\n".join(x for x in (r.get("title"), bills and "Bills: " + bills,
                                     r.get("details_url")) if x)
        lines += ["SUMMARY:" + _ics_text(r.get("committee") or r.get("title")),
                  "DESCRIPTION:" + _ics_text(desc)]
        if r.get("location"):
            lines.append("LOCATION:" + _ics_text(r["location"]))
        if r.get("details_url"):
            lines.append("URL:" + r["details_url"])
        if r.get("status") == "canceled":
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(x) for x in lines) + "\r\n"


def write_downloads(data_dir, out_dir, names, now):
    """site/downloads/: hearings-<code>.csv (every meeting on file, past included),
    hearings-all.csv, <code>.ics (upcoming), manifest.json. Returns the manifest."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    today = now.strftime("%Y-%m-%d")
    every, manifest = [], {"license": "CC BY 4.0 (credit Govbot Public Hearings)",
                           "places": []}
    for d in sorted(p for p in Path(data_dir).iterdir() if p.is_dir()):
        rows = []
        for f in d.glob("*.json"):
            try:
                rows.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                pass
        if not rows:
            continue
        rows.sort(key=lambda r: (r.get("scheduled_iso") or "9999", r["id"]))
        code = d.name
        name = names.get(code, code.upper())
        upcoming = [r for r in rows if (r.get("scheduled_iso") or "")[:10] >= today]
        (out / f"hearings-{code}.csv").write_text(hearings_csv(rows, names), encoding="utf-8")
        (out / f"{code}.ics").write_text(hearings_ics(upcoming, f"{name} hearings · Govbot"),
                                         encoding="utf-8")
        every += rows
        manifest["places"].append({"code": code, "name": name, "meetings": len(rows),
                                   "upcoming": len(upcoming),
                                   "csv": f"hearings-{code}.csv", "ics": f"{code}.ics"})
    (out / "hearings-all.csv").write_text(hearings_csv(every, names), encoding="utf-8")
    manifest["meetings"] = len(every)
    (out / "manifest.json").write_text(_dump(manifest))
    return manifest


# --------------------------------------------------------------------------- #
# offline (snapshot) build — no network
# --------------------------------------------------------------------------- #
def build_from_fixtures(fixtures_dir):
    """Deterministic build straight from raw __snapshots__ fixtures."""
    d = Path(fixtures_dir)
    hearings = []
    il_files = [("il_house.json", "house"), ("il_active.json", "house"),
                ("il_senate.json", "senate")]
    for fname, chamber in il_files:
        p = d / fname
        if p.exists():
            hearings.extend(parse_il_hearings(p.read_text(), chamber))
    wa_path = d / "wa_meetings.xml"
    if wa_path.exists():
        for m in parse_wa_meetings(wa_path.read_text()):
            items_file = d / f"wa_items_{m['_agenda_id']}.xml"
            ids = parse_wa_items(items_file.read_text()) if items_file.exists() else []
            hearings.append(wa_attach_bills(m, ids))
    # Massachusetts: a list stub file names the detail fixtures to parse.
    ma_list = d / "ma_hearings.json"
    if ma_list.exists():
        for eid in parse_ma_hearing_list(ma_list.read_text()):
            detail = d / f"ma_hearing_{eid}.json"
            if detail.exists():
                rec = parse_ma_hearing(detail.read_text())
                if rec:
                    hearings.append(rec)
    # Alaska: one BASIS meetings document.
    ak_path = d / "ak_meetings.json"
    if ak_path.exists():
        hearings.extend(parse_ak_meetings(ak_path.read_text()))
    # Chicago: one real eLMS /meeting-agenda response.
    chi_path = d / "chi_meetings.json"
    if chi_path.exists():
        hearings.extend(parse_chi_meetings(chi_path.read_text()))
    # Federal: a trimmed real Federal Register response (deterministic).
    fr_path = d / "fr_documents.json"
    if fr_path.exists():
        hearings.extend(parse_fr_documents(fr_path.read_text()))
    return hearings


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def assemble(hearings, jurisdictions, source, now):
    # Only upcoming hearings: drop anything whose date is before today. A hearing
    # with an unparseable time (scheduled_iso None) is kept — we can't prove it's
    # past. "Today" uses the build date so a hearing earlier today still shows.
    today = now.strftime("%Y-%m-%d")
    hearings = [h for h in hearings
                if not h.get("scheduled_iso") or h["scheduled_iso"][:10] >= today]
    # Federal: only the soonest-closing share is kept; the rest is one click away
    # (open_total + full_list_url ride on the federal jurisdiction entry).
    extra = {}
    fed = [h for h in hearings if h["jurisdiction"] == "us"]
    if fed:
        shown, extra["us"] = federal_preview(fed, now.date())
        hearings = [h for h in hearings if h["jurisdiction"] != "us"] + shown
    # USA (Federal) leads, then the rest alphabetically — the dashboard shows the
    # federal comment periods above the state hearings.
    rank = lambda code: ({"us": 0, "usa": 1}.get(code, 2), code)
    used = [j for j in jurisdictions if any(h["jurisdiction"] == j for h in hearings)] \
        or list(jurisdictions)
    used = sorted(used, key=rank)
    hearings = sorted(hearings, key=lambda h: (
        rank(h["jurisdiction"]), h["scheduled_iso"] or "9999", h["id"]))
    counts = {}
    for h in hearings:
        counts[h["jurisdiction"]] = counts.get(h["jurisdiction"], 0) + 1
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "jurisdictions": [
            {**{k: v for k, v in JURISDICTIONS[j].items()
                if k in ("code", "name", "participation", "portal_url", "full_list_url", "how_to")},
             **extra.get(j, {})}
            for j in used if j in JURISDICTIONS
        ],
        "counts": counts,
        "hearings": hearings,
    }


# --------------------------------------------------------------------------- #
# RSS feed
# --------------------------------------------------------------------------- #
# Public URL where the dashboard (and this feed) live, so readers resolve links.
SITE_URL = "https://govbot-public-hearings.vercel.app/"
FEED_URL = SITE_URL + "hearings.xml"

# Attaching this XSLT makes a browser render the feed as a readable page instead
# of a raw "this XML has no style information" tree (feed readers ignore it and
# parse the RSS as usual). The stylesheet lives at dashboard/feed.xsl, so the
# whole-calendar feed at the dashboard root references "feed.xsl" and a granular
# feed one directory down references "../feed.xsl". See site/feed.xsl.
FEED_XSL = "feed.xsl"


def _xml_prolog(xsl_href):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<?xml-stylesheet type="text/xsl" href="{xsl_href}"?>\n')


def _pretty_when(h):
    """Human 'Aug 4, 2026, 10:00 AM' from scheduled_iso, else the raw display."""
    s = h.get("scheduled_iso") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", s)
    if not m:
        return h.get("scheduled_display") or "Time TBA"
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]
    hh = int(m.group(4))
    ampm = "PM" if hh >= 12 else "AM"
    hh = (hh + 11) % 12 + 1
    return (f"{months[int(m.group(2)) - 1]} {int(m.group(3))}, {m.group(1)}, "
            f"{hh}:{m.group(5)} {ampm}")


# Feed dates are published in Central time (CST/CDT) to match the govbot
# dashboard's home turf; zoneinfo is DST-aware, with a UTC fallback if the IANA
# database is unavailable (still a valid RFC-822 date, just not localized).
try:
    from zoneinfo import ZoneInfo
    FEED_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata unavailable
    FEED_TZ = timezone.utc


def _feed_prelude(doc):
    """Shared (built_822, names) derived from a hearings doc for feed rendering.
    built_822 is expressed in Central time."""
    from email.utils import format_datetime
    built = datetime.strptime(doc["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    names = {j["code"]: j["name"] for j in doc.get("jurisdictions", [])}
    return format_datetime(built.astimezone(FEED_TZ)), names


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _add_item(ch, h, state, built_822):
    """Append one <item> for hearing `h` to channel `ch`.

    The title leads with what the hearing is about (its subject, or the
    committee) and when; the description is a few labelled lines of HTML
    (readers and the browser stylesheet render it) so a reader shows the
    place, the bills with their titles and the way to take part without
    opening anything."""
    canceled = h.get("status") == "canceled"
    status = " [CANCELED]" if canceled else ""
    committee = h.get("committee") or "Committee"
    subject = (h.get("title") or "").strip()
    # ILGA titles read "SUBJECT MATTER:  HB 1643"; the bills line already says so.
    if re.match(r"(?i)subject matter:", subject):
        subject = ""
    what = subject if subject and subject.lower() != committee.lower() else committee
    # Widgets that embed a feed show only the title, so it says it all on one
    # line: the state, what and who, when, and (state hearings) the bills.
    bill_ids = [b["id"] for b in h.get("bills", [])]
    who = f" ({committee})" if what != committee else ""
    on = f" · {', '.join(bill_ids)}" if bill_ids and h.get("jurisdiction") != "us" else ""
    when = _pretty_when(h)
    # A federal date is a comment deadline, not a meeting time.
    if h.get("jurisdiction") == "us":
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", h.get("scheduled_iso") or "")
        if m:
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                      "Oct", "Nov", "Dec"]
            when = f"Comments due {months[int(m.group(2)) - 1]} {int(m.group(3))}, {m.group(1)}"
    title = f"{state} · {what}{who} — {when}{status}{on}"

    lines = []
    if canceled:
        lines.append("<b>Canceled.</b>")
    if subject and what != committee:
        lines.append(f"<b>Committee:</b> {_esc(committee)}")
    lines.append(f"<b>Where:</b> {_esc(state)}")
    lines.append(f"<b>When:</b> {_esc(when)}")
    if h.get("location"):
        lines.append(f"<b>Place:</b> {_esc(h['location'])}")
    bills = h.get("bills", [])
    if bills:
        parts = []
        for b in bills:
            label = _esc(b["id"])
            if b.get("title") and b["title"] != h.get("title"):
                label += f" — {_esc(b['title'])}"
            parts.append(f'<a href="{_esc(b["url"])}">{label}</a>' if b.get("url") else label)
        lines.append(f"<b>Bill{'s' if len(bills) > 1 else ''}:</b> " + "; ".join(parts))
    if not canceled and h.get("witness_slip_url"):
        u = _esc(h["witness_slip_url"])
        lines.append(f'<b>Take part:</b> <a href="{u}">{u}</a>')
    if h.get("details_url"):
        u = _esc(h["details_url"])
        lines.append(f'<b>Details:</b> <a href="{u}">{u}</a>')

    item = ET.SubElement(ch, "item")
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "link").text = h.get("details_url") or SITE_URL
    ET.SubElement(item, "description").text = "<br/>".join(lines)
    ET.SubElement(item, "category").text = state
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = h["id"]
    ET.SubElement(item, "pubDate").text = built_822


def _feed_xml(title, description, self_url, hearings, names, built_822,
              xsl_href="../" + FEED_XSL):
    """Render an RSS 2.0 channel (one <item> per hearing) as a string."""
    rss = ET.Element("rss", {"version": "2.0",
                             "xmlns:atom": "http://www.w3.org/2005/Atom"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = SITE_URL
    ET.SubElement(ch, "description").text = description
    ET.SubElement(ch, "language").text = "en-us"
    ET.SubElement(ch, "lastBuildDate").text = built_822
    ET.SubElement(ch, "atom:link", {"href": self_url, "rel": "self",
                                    "type": "application/rss+xml"})
    for h in hearings:
        state = names.get(h["jurisdiction"], h["jurisdiction"].upper())
        _add_item(ch, h, state, built_822)
    return _xml_prolog(xsl_href) + ET.tostring(rss, encoding="unicode") + "\n"


def to_rss(doc):
    """Render the whole hearings document as an RSS 2.0 feed (one item per
    hearing). Pure/deterministic given `doc`. Feed readers subscribe to this to
    get every upcoming committee hearing without visiting the dashboard.
    """
    built_822, names = _feed_prelude(doc)
    return _feed_xml(
        "govbot — Upcoming committee hearings & witness slips",
        "Upcoming legislative committee hearings where the public can weigh in "
        "(Illinois, Washington, Massachusetts, Alaska + federal rules), refreshed every 12 hours by Govbot.",
        FEED_URL, doc.get("hearings", []), names, built_822, xsl_href=FEED_XSL)


def bill_feed_name(jurisdiction, bill_id):
    """Deterministic per-bill feed filename, e.g. ('il', 'HB 1643') -> 'il-HB1643.xml'.
    The hearings page recomputes this exact name to link each bill to its feed,
    so both sides must normalize identically (see _norm_bill_id)."""
    return f"{jurisdiction}-{_norm_bill_id(bill_id)}.xml"


def bill_feeds(doc):
    """One RSS feed per distinct bill that appears in any hearing.

    Returns {filename: xml_string}. Each feed lists just the hearings that
    reference that bill, so a reader can follow a single bill instead of the
    whole calendar. Pure/deterministic given `doc`.
    """
    built_822, names = _feed_prelude(doc)
    # Group hearings by (jurisdiction, normalized id); keep the first display id
    # seen so the human-facing title reads naturally.
    groups = {}
    for h in doc.get("hearings", []):
        for b in h.get("bills", []):
            key = (h["jurisdiction"], _norm_bill_id(b["id"]))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"jur": h["jurisdiction"], "id": b["id"], "hearings": []}
            g["hearings"].append(h)

    feeds = {}
    for g in groups.values():
        state = names.get(g["jur"], g["jur"].upper())
        disp = g["id"]
        fname = bill_feed_name(g["jur"], disp)
        self_url = SITE_URL + "hearings/" + fname
        feeds[fname] = _feed_xml(
            f"govbot — {state} {disp}: upcoming committee hearings",
            f"Upcoming committee hearings featuring {state} {disp}, "
            f"refreshed every 12 hours by Govbot.",
            self_url, g["hearings"], names, built_822)
    return feeds


def jurisdiction_feed_name(code):
    """Per-state feed filename, e.g. 'wa' -> 'wa.xml'. No hyphen, so it never
    collides with a per-bill '<jur>-<ID>.xml' or a per-hearing 'hearing-*.xml'."""
    return f"{code}.xml"


def jurisdiction_feeds(doc):
    """One RSS feed per jurisdiction (all of that state's upcoming hearings), so
    a state whose hearings list no individual bills (e.g. WA committee meetings)
    is still followable. Returns {filename: xml_string}."""
    built_822, names = _feed_prelude(doc)
    by_code = {}
    for h in doc.get("hearings", []):
        by_code.setdefault(h["jurisdiction"], []).append(h)
    feeds = {}
    for code, hearings in by_code.items():
        state = names.get(code, code.upper())
        fname = jurisdiction_feed_name(code)
        feeds[fname] = _feed_xml(
            f"govbot — {state}: upcoming committee hearings",
            f"All upcoming {state} legislative committee hearings, "
            f"refreshed every 12 hours by Govbot.",
            SITE_URL + "hearings/" + fname, hearings, names, built_822)
    return feeds


def hearing_feed_name(hearing_id):
    """Per-hearing feed filename, e.g. 'wa-other-33551' -> 'hearing-wa-other-33551.xml'.
    The 'hearing-' prefix keeps it distinct from per-bill/per-state names; other
    characters are sanitized to stay filesystem/URL safe."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", hearing_id or "")
    return f"hearing-{safe}.xml"


def hearing_feeds(doc):
    """One RSS feed per individual hearing (a single item that updates if the
    hearing is rescheduled or canceled), so any committee meeting is followable
    on its own. Returns {filename: xml_string}."""
    built_822, names = _feed_prelude(doc)
    feeds = {}
    for h in doc.get("hearings", []):
        state = names.get(h["jurisdiction"], h["jurisdiction"].upper())
        fname = hearing_feed_name(h["id"])
        feeds[fname] = _feed_xml(
            f"govbot — {state} {h.get('committee', 'committee')}: {_pretty_when(h)}",
            f"A single {state} committee hearing tracked by govbot; the item "
            f"updates if it is rescheduled or canceled.",
            SITE_URL + "hearings/" + fname, [h], names, built_822)
    return feeds


def participation_feeds(doc, participation_path):
    """An empty-but-valid per-jurisdiction feed for every participation-directory
    state that has no live feed yet, so a reader can subscribe to their state
    now and get items the moment it starts posting hearings. Never overwrites a
    populated jurisdiction feed. Fail-soft: a missing/bad participation file
    contributes nothing. Returns {filename: xml_string}."""
    try:
        data = json.loads(Path(participation_path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    built_822, names = _feed_prelude(doc)
    have = {h["jurisdiction"] for h in doc.get("hearings", [])}
    feeds = {}
    for j in data.get("jurisdictions", []):
        code = j.get("code")
        if not code or code in have:
            continue  # populated feed already produced by jurisdiction_feeds
        state = j.get("name", code.upper())
        fname = jurisdiction_feed_name(code)
        feeds[fname] = _feed_xml(
            f"govbot — {state}: upcoming committee hearings",
            f"Upcoming {state} legislative committee hearings. No hearings are "
            f"in govbot's window yet; subscribe now to get them when {state} "
            f"starts posting. Refreshed every 12 hours.",
            SITE_URL + "hearings/" + fname, [], names, built_822)
    return feeds


def all_feeds(doc, participation_path=None):
    """Every per-entity feed to publish alongside the whole-calendar hearings.xml:
    one per bill, one per jurisdiction, one per hearing, and (when a participation
    directory is given) an empty placeholder feed for every not-yet-live state."""
    feeds = {}
    feeds.update(bill_feeds(doc))
    feeds.update(jurisdiction_feeds(doc))
    feeds.update(hearing_feeds(doc))
    if participation_path:
        # Placeholders first so a real populated jurisdiction feed always wins.
        placeholders = participation_feeds(doc, participation_path)
        placeholders.update(feeds)
        feeds = placeholders
    return feeds


# --------------------------------------------------------------------------- #
# govbot bill enrichment
# --------------------------------------------------------------------------- #
def _norm_bill_id(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def enrich_from_govbot(hearings, data_path):
    """Cross-reference each hearing's bills against govbot's own bill dataset
    (its per-state bills/<code>.json files, merged into one data.json) and attach the govbot title + topic tags where
    the bill is tracked. IL and WA are both scraped by govbot, so their hearing
    bills resolve to real govbot records. Fail-soft: a missing/partial data.json
    (e.g. the offline sample) simply means no enrichment."""
    try:
        data = json.loads(Path(data_path).read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    index = {}
    for b in data.get("bills", []):
        st = (b.get("state") or "").lower()
        index[(st, _norm_bill_id(b.get("id")))] = b
    matched = 0
    for h in hearings:
        for bill in h.get("bills", []):
            rec = index.get((h["jurisdiction"], _norm_bill_id(bill["id"])))
            if not rec:
                continue
            title = (rec.get("title") or "").strip()
            if title:
                bill["govbot_title"] = title
            tags = [t for t in (rec.get("tags") or []) if t]
            if tags:
                bill["govbot_tags"] = tags
            # A bill with no link of its own gets its official page from the Govbot record
            # (each record's url is the legislature's own bill page).
            if not bill.get("url") and (rec.get("url") or "").startswith(("https://", "http://")):
                bill["url"] = rec["url"]
            if title or tags:
                matched += 1
    return matched


# --------------------------------------------------------------------------- #
# Place pages: one short, plain-HTML answer page per place (AEO / GEO)
# --------------------------------------------------------------------------- #
# The map is drawn by JavaScript, which most search and AI crawlers don't run. So every
# refresh also writes site/places/<slug>.html: one focused page per place that opens with
# the answer ("To weigh in at a Virginia legislative hearing, ..."), then numbered steps,
# the upcoming hearings, a short FAQ, and schema.org labels (FAQPage, HowTo, Event,
# BreadcrumbList) saying what each part is. Plus site/sitemap.xml. All outside text is
# escaped (html.escape; JSON-LD with "<" escaped so it can't close the script tag).
ACTION = {
    "witness_slip": "file an online witness slip before the hearing starts",
    "committee_sign_in": "sign in online for that committee meeting to testify or record your position",
    "written_testimony": "send written testimony to the committee before the hearing",
    "public_comment": "send a public comment through the official form before the deadline",
    "request_to_speak": "register through the legislature's Request to Speak system",
    "appearance_form": "fill out the committee's appearance form",
    "position_letter": "send a position letter to the committee",
    "contact_only": "testify in person or write to the committee chair before the hearing",
}
STEP = {
    "witness_slip": "Fill in the slip: who you are, your position (for, against or no position) and whether you want to testify. It closes when the hearing starts, and it is public.",
    "committee_sign_in": "Sign in for that meeting and choose whether to testify or only note your position.",
    "written_testimony": "Write your testimony, name the bill number, and send it to the committee before the hearing.",
    "public_comment": "Write your comment, name the bill or topic, and send it through the official form before the deadline.",
    "request_to_speak": "Find the agenda item, then record your position or ask to speak.",
    "appearance_form": "Fill out the appearance form to speak or to put your position on the record.",
    "position_letter": "Write a short letter naming the bill number and your position, and send it to the committee.",
    "contact_only": "Email the committee chair before the hearing, or go to the hearing and testify in person.",
}
SLUG = {"us": "federal", "usa": "congress", "chi": "chicago"}
PAGES_MAX = 40          # hearings listed (and labelled as Events) per place page


def place_slug(code, name):
    return SLUG.get(code) or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _jsonld(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1).replace("<", "\\u003c")


def _human_time(h):
    iso = h.get("scheduled_iso") or ""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)", iso)
    if not m:
        return h.get("scheduled_display") or "Time to be announced"
    y, mo, d, hh, mm = (int(x) for x in m.groups())
    day = f"{_MONTHS[mo - 1]} {d}, {y}"
    if "time not posted" in (h.get("scheduled_display") or ""):
        return day
    return f"{day}, {(hh + 11) % 12 + 1}:{mm:02d} {'PM' if hh >= 12 else 'AM'}"


def _event_ld(h, name):
    from zoneinfo import ZoneInfo
    iso = h.get("scheduled_iso")
    start = iso[:10]
    if iso and "time not posted" not in (h.get("scheduled_display") or ""):
        try:
            start = datetime.fromisoformat(iso).replace(
                tzinfo=ZoneInfo(h.get("timezone") or "America/Chicago")).isoformat()
        except (ValueError, KeyError):
            pass
    ev = {"@type": "Event", "name": h.get("committee") or h.get("title") or "Public hearing",
          "startDate": start, "isAccessibleForFree": True,
          "eventStatus": "https://schema.org/Event" + ("Cancelled" if h.get("status") == "canceled" else "Scheduled"),
          "organizer": {"@type": "GovernmentOrganization", "name": name}}
    if h.get("title"):
        ev["description"] = h["title"][:300]
    if h.get("location"):
        ev["location"] = {"@type": "Place", "name": h["location"], "address": h["location"]}
    if h.get("details_url"):
        ev["url"] = h["details_url"]
    return ev


PART_LABEL = {"witness_slip": "File a witness slip", "committee_sign_in": "Sign in to testify",
              "written_testimony": "Submit written testimony", "public_comment": "Submit a public comment",
              "request_to_speak": "Request to speak", "appearance_form": "Fill out an appearance form",
              "position_letter": "Send a position letter"}


def place_page(p, hearings, generated_at, has_downloads, tips=()):
    """One place's page as HTML (pure). p: {code, name, participation, portal_url, how_to}."""
    import html as H
    e = lambda s: H.escape(s or "", quote=True)
    code, name = p["code"], p["name"]
    slug = place_slug(code, name)
    url = SITE_URL + f"places/{slug}.html"
    kind = p.get("participation") or "contact_only"
    up = [h for h in hearings if h["jurisdiction"] == code and h.get("status") != "canceled"]
    shown = up[:PAGES_MAX]
    asof = generated_at[:10]
    if code == "usa":
        q1 = "How do I weigh in on a congressional hearing?"
        lead = ("To weigh in on a congressional hearing, contact your representative and senators "
                "before it, or send a written statement for the hearing record where the committee "
                "accepts one. Witnesses who testify are invited by the committee.")
        steps = ["Find the hearing or markup for the bill or topic you care about (upcoming ones are listed below).",
                 "Find your representative and senators: https://www.congress.gov/members/find-your-member",
                 "Call, email or write to them before the hearing, naming the bill number and your position.",
                 "If the committee accepts statements from the public for the record, send yours by its deadline (each committee's site says how)."]
        title = "How to weigh in on a congressional hearing · Govbot Public Hearings"
        h1 = "How to weigh in on a congressional hearing"
        what = "hearings"
    elif code == "us":
        q1 = "How do I comment on a federal rule?"
        lead = ("To comment on a federal rule, open it on Regulations.gov and submit your comment "
                "before its comment deadline. Comments are public and the agency must consider them.")
        steps = ["Find a proposed rule open for comment (the soonest-closing are listed below).",
                 "Open it on Regulations.gov and choose Comment.",
                 "Say what you support or oppose and why, with facts from your own experience, then submit before the deadline."]
        title = "How to comment on a federal rule · Govbot Public Hearings"
        h1 = "How to comment on a federal rule"
        what = "comment periods"
    else:
        body = "City Council meeting" if code == "chi" else "legislative hearing"
        q1 = f"How do I take part in a {name} {body}?"
        action = ("send a written comment by the deadline in the meeting's notice"
                  if code == "chi" else ACTION.get(kind, ACTION["contact_only"]))
        lead = f"To weigh in at a {name} {body}, {action}."
        where = (f"Open {name}'s official form for taking part: {p['participate_url']}" if p.get("participate_url")
                 else f"Follow the hearing notice; {name}'s official legislature site is {p.get('portal_url')}")
        steps = [f"Find the hearing for the bill or topic you care about (upcoming {name} hearings are listed below).",
                 where,
                 STEP.get(kind, STEP["contact_only"]),
                 "Check the hearing notice for its exact rules and deadline."]
        title = f"How to testify at a {name} hearing · Govbot Public Hearings"
        h1 = f"How to take part in a {name} {body}"
        what = "hearings"
    if up:
        first = up[0]
        q2a = (f"{len(up)} {what} are posted for the next few weeks. The soonest is "
               f"{first.get('committee') or first.get('title')} on {_human_time(first)}.")
    else:
        q2a = (f"None are posted right now. Legislatures post hearings a few days to a few weeks "
               f"ahead, mostly while in session. Follow {name} by RSS to hear when one is.")
    src = sorted({h.get("source") for h in up if h.get("source")})
    q3a = (f"From {', '.join(src) if src else 'the official ' + name + ' website'}"
           f", the government's own site. Every hearing links to its official page. "
           f"Updated every 12 hours; last updated {asof}.")
    faq = [(q1, lead), (f"When are the next {name} {what}?", q2a),
           ("Where does this information come from?", q3a)]
    if has_downloads:
        faq.append((f"Can I add {name} hearings to my calendar?",
                    f"Yes. Subscribe to {SITE_URL}downloads/{code}.ics in Google, Apple or Outlook "
                    f"calendar, or download every {name} hearing as a spreadsheet (CSV). Free, CC BY 4.0."))
    ld = [
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq]},
        {"@context": "https://schema.org", "@type": "HowTo", "name": h1,
         "step": [{"@type": "HowToStep", "position": i + 1, "text": s} for i, s in enumerate(steps)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Govbot Public Hearings", "item": SITE_URL},
            {"@type": "ListItem", "position": 2, "name": name, "item": url}]},
    ]
    if shown:
        ld.append({"@context": "https://schema.org", "@type": "ItemList",
                   "name": f"Upcoming {name} {what}",
                   "itemListElement": [{"@type": "ListItem", "position": i + 1,
                                        "item": _event_ld(h, name)} for i, h in enumerate(shown)]})
    rows = []
    for h in shown:
        link = h.get("details_url") or h.get("witness_slip_url")
        cal = h.get("calendar_url") if not link else None
        bills = ", ".join(b["id"] for b in h.get("bills", [])[:12])
        rows.append(
            "<li><strong>" + e(_human_time(h)) + "</strong> · " + e(h.get("committee") or h.get("title"))
            + (f"<br><span>{e(h['title'][:240])}</span>" if h.get("title") and h.get("title") != h.get("committee") else "")
            + (f"<br><span>{e(h['location'])}</span>" if h.get("location") else "")
            + (f"<br><span>Bills: {e(bills)}</span>" if bills else "")
            + (f'<br><a href="{e(link)}" rel="noopener">This hearing on the official site ↗</a>' if link else "")
            + (f'<br><a href="{e(cal)}" rel="noopener">Source: the official meeting calendar ↗</a>' if cal else "")
            + "</li>")
    more = (f'<p>Showing {len(shown)} of {len(up)}. <a href="{e(SITE_URL)}#hg-{e(code)}">See them all</a>.</p>'
            if len(up) > len(shown) else "")
    dl = (f'<p><a href="../downloads/{e(code)}.ics">Add all {e(name)} hearings to my calendar (.ics)</a> · '
          f'<a href="../downloads/hearings-{e(code)}.csv">Spreadsheet (CSV)</a> · '
          f'<a href="../hearings/{e(code)}.xml">RSS</a></p>') if has_downloads else \
        f'<p><a href="../hearings/{e(code)}.xml">Follow by RSS</a></p>'
    desc = lead + (" " + q2a.split(". ")[0] + "." if up else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc[:300])}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{e(h1)}">
<meta property="og:description" content="{e(lead)}">
<meta property="og:url" content="{e(url)}">
<meta property="og:image" content="{e(SITE_URL)}assets/og-hearings.png">
<link rel="alternate" type="application/rss+xml" title="{e(name)} hearings" href="../hearings/{e(code)}.xml">
<style>
:root {{ --paper: #FBFAF5; --ink: #1B1A17; --line: #8E8B82; --link: #1747C9; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --paper: #1B1A17; --ink: #F3F1EA; --line: #8E8B82; --link: #8AB4FF; }} }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font: 18px/1.55 Roboto, system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 24px 16px 56px; overflow-wrap: anywhere; }}
a {{ color: var(--link); text-underline-offset: 3px; }}
h1 {{ font-size: 34px; line-height: 1.15; margin: 18px 0 12px; font-weight: 500; }}
h2 {{ font-size: 22px; margin: 32px 0 8px; font-weight: 500; border-bottom: 1.5px solid var(--line); padding-bottom: 4px; }}
h3 {{ font-size: 18px; margin: 18px 0 4px; }}
.lead {{ font-size: 21px; }}
ol, ul {{ padding-left: 22px; }}
li {{ margin: 0 0 12px; }}
li span {{ font-size: 16px; }}
nav, footer {{ font-size: 15px; }}
footer {{ margin-top: 40px; border-top: 1.5px solid var(--line); padding-top: 12px; }}
</style>
<script type="application/ld+json">
{_jsonld(ld)}
</script>
</head>
<body>
<main>
<nav><a href="../">Govbot Public Hearings</a> › {e(name)}</nav>
<h1>{e(h1)}</h1>
<p class="lead">{e(lead)}</p>
{('<p>' + e(p['how_to']) + '</p>') if p.get('how_to') else ''}
<p><a href="{e(p.get('participate_url') or p.get('portal_url'))}" rel="noopener">{e(PART_LABEL.get(p.get('participation')) if p.get('participate_url') else 'Official ' + name + ' legislature site')} ↗</a></p>
<h2>Steps</h2>
<ol>{''.join('<li>' + e(s) + '</li>' for s in steps)}</ol>
{('<h2>Tips</h2><ul>' + ''.join('<li>' + e(t) + '</li>' for t in tips) + '</ul>') if tips else ''}
<h2>Upcoming {e(name)} {what}</h2>
{('<ul>' + ''.join(rows) + '</ul>') if rows else '<p>' + e(q2a) + '</p>'}
{more}{dl}
<h2>FAQs</h2>
{''.join('<h3>' + e(q) + '</h3><p>' + e(a) + '</p>' for q, a in faq)}
<footer>
<p>Data as of {e(asof)}, from each government's own website. Built by <a href="https://chihacknight.github.io/govbot/" rel="noopener">Govbot</a>, an open-source civic data project from Chi Hack Night. <a href="https://github.com/frankies2727/govbot-public-hearings" rel="noopener">Data and code on GitHub</a> (CC BY 4.0).</p>
<p><a href="../">See every place on the map</a></p>
</footer>
</main>
</body>
</html>
"""


def write_place_pages(doc, participation_path, out_dir, sitemap_path, downloads_dir=None):
    """Write site/places/<slug>.html for every place + site/sitemap.xml. Returns the count."""
    places = participation_jurisdictions(participation_path)
    try:
        pj = json.loads(Path(participation_path).read_text())
        rows = pj.get("jurisdictions", [])
        how = {r["code"]: r.get("how_to", "") for r in rows}
        tool = {r["code"]: r["participate_url"] for r in rows if r.get("participate_url")}
        tips = pj.get("tips", [])
    except (OSError, ValueError):
        how, tool, tips = {}, {}, []
    for j in list(doc.get("jurisdictions", [])) + list(JURISDICTIONS.values()):
        places.setdefault(j["code"], {k: j[k] for k in ("code", "name", "participation", "portal_url")})
        if j.get("how_to"):
            how.setdefault(j["code"], j["how_to"])
    dl = set()
    if downloads_dir and (Path(downloads_dir) / "manifest.json").exists():
        dl = {m["code"] for m in json.loads((Path(downloads_dir) / "manifest.json").read_text())["places"]}
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    urls = [SITE_URL]
    for code, p in sorted(places.items(), key=lambda kv: kv[1]["name"]):
        p = dict(p, how_to=how.get(code, ""), participate_url=tool.get(code))
        slug = place_slug(code, p["name"])
        (out / f"{slug}.html").write_text(
            place_page(p, doc.get("hearings", []), doc["generated_at"], code in dl, tips), encoding="utf-8")
        urls.append(SITE_URL + f"places/{slug}.html")
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"  <url><loc>{u}</loc></url>" for u in urls]
    sm.append("</urlset>")
    Path(sitemap_path).write_text("\n".join(sm) + "\n")
    return len(urls) - 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jurisdictions", default="il,wa,ma,ak,chi,ia,usa,us",
                    help="Comma-separated codes to scrape live (default: il,wa,ma,ak,chi,us; "
                         "'us' = federal comment periods from Regulations.gov)")
    ap.add_argument("--from-fixtures", default=None,
                    help="Build offline from a raw fixtures dir (no network)")
    ap.add_argument("--slips", action="store_true",
                    help="Attempt best-effort witness-slip count enrichment "
                         "(opt-in; the IL totals endpoint is currently unreliable)")
    ap.add_argument("--now", default=None,
                    help="Override generated_at (ISO, e.g. 2026-08-28T00:00:00Z) "
                         "— used for deterministic snapshots")
    ap.add_argument("--output", "-o", default="-", help="Output path (default: stdout)")
    ap.add_argument("--rss", default=None,
                    help="Also write an RSS 2.0 feed of the hearings to this path")
    ap.add_argument("--rss-feeds-dir", default=None,
                    help="Also write granular RSS 2.0 feeds into this directory: one "
                         "per bill (il-HB1643.xml), one per jurisdiction (wa.xml), and "
                         "one per hearing (hearing-<id>.xml), so readers can follow a "
                         "single bill, a whole state, or one hearing instead of the "
                         "entire calendar")
    ap.add_argument("--dashboard-data", default="",
                    help="govbot bill dataset (data.json) used to enrich hearing "
                         "bills with their govbot title + topic tags")
    ap.add_argument("--os-events", default=None,
                    help="Folder of Open States meeting-scraper output (<code>/event_*.json "
                         "plus an _ok file when that place's scraper finished): adds every "
                         "place in it to the live scrape")
    ap.add_argument("--data-dir", default=None,
                    help="Keep every meeting in this folder (data/<code>/<id>.json, a history "
                         "anyone can clone); a place whose scraper broke shows its stored "
                         "upcoming meetings instead of none")
    ap.add_argument("--downloads-dir", default=None,
                    help="Write CSV + iCalendar downloads of --data-dir into this folder")
    ap.add_argument("--places-dir", default=None,
                    help="Write one plain-HTML answer page per place here (site/places) and "
                         "a sitemap beside it, for search and AI answer engines")
    ap.add_argument("--participation", default=None,
                    help="participation.json — when given, an empty placeholder "
                         "per-jurisdiction RSS feed is written for every listed "
                         "state that has no live hearings yet, so residents can "
                         "subscribe before their state starts posting")
    args = ap.parse_args()

    now = (datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
           if args.now else datetime.now(timezone.utc))

    if args.from_fixtures:
        hearings = build_from_fixtures(args.from_fixtures)
        source = "fixtures (offline snapshot)"
        codes = ["us", "il", "wa", "ma", "ak", "chi"]
    else:
        codes = [c.strip().lower() for c in args.jurisdictions.split(",") if c.strip()]
        begin = now - timedelta(days=WINDOW_BACK_DAYS)
        end = now + timedelta(days=WINDOW_FWD_DAYS)
        hearings = []
        if "il" in codes:
            hearings.extend(fetch_il(begin, end, with_slips=args.slips))
        if "wa" in codes:
            hearings.extend(fetch_wa(begin, end))
        if "ma" in codes:
            hearings.extend(fetch_ma(begin, end))
        if "ak" in codes:
            hearings.extend(fetch_ak(begin, end))
        if "chi" in codes:
            hearings.extend(fetch_chi(begin, end))
        if "ia" in codes:                    # Iowa's public hearings (its committee meetings come from Open States)
            hearings.extend(fetch_ia_hearings())
        if "usa" in codes:                   # Congress's committee hearings
            hearings.extend(fetch_usa(now))
        if "us" in codes:                    # federal rules open for comment
            hearings.extend(fetch_us(now.date()))
        source = ("ilga.gov + leg.wa.gov + malegislature.gov + akleg.gov + "
                  "chicityclerkelms.chicago.gov + legis.iowa.gov + senate.gov + docs.house.gov + "
                  "federalregister.gov (govbot scrape-hearings)")
        if args.os_events:
            # Places we scrape ourselves keep our own scraper; Open States fills the rest.
            os_codes = sorted(p.name for p in Path(args.os_events).iterdir()
                              if p.is_dir() and p.name not in OS_SKIP)
            os_hearings, os_ok = load_os_events(args.os_events, os_codes)
            hearings.extend(os_hearings)
            codes += [c for c in os_codes if c not in codes]
            source += " + each legislature's own site via Open States' meeting scrapers"
            print(f"Open States meetings: {len(os_hearings)} from {len(os_ok)} of "
                  f"{len(os_codes)} places", file=sys.stderr)
            if args.data_dir:
                broken = [c for c in os_codes if c not in os_ok]
                # A scraper cut off partway still left some meetings; add only the stored ones
                # it didn't get to, so nothing shows twice.
                have = {h["id"] for h in hearings}
                kept = [h for h in restore_from_archive(args.data_dir, broken, now)
                        if h["id"] not in have]
                hearings.extend(kept)
                if kept:
                    print(f"kept {len(kept)} stored meetings for places whose scraper broke",
                          file=sys.stderr)
                late = update_status(args.data_dir, os_codes, os_ok, os_hearings, now)
                for c in late:
                    print(f"::warning::the {c} meeting scraper has failed 3+ runs in a row",
                          file=sys.stderr)

    if args.participation:
        for code, j in participation_jurisdictions(args.participation).items():
            JURISDICTIONS.setdefault(code, j)

    if args.dashboard_data:
        n = enrich_from_govbot(hearings, args.dashboard_data)
        if n:
            print(f"enriched {n} hearing bills from govbot data", file=sys.stderr)

    if args.data_dir and not args.from_fixtures:
        # Our own scrapers can't tell "none posted" from "site down", so their places
        # only ever add to the archive; Open States places also prune (see archive_hearings).
        ok = set(os_ok) if args.os_events else set()
        n = archive_hearings(hearings, args.data_dir, ok, now)
        print(f"data: {n} meeting files written to {args.data_dir}", file=sys.stderr)
        if args.downloads_dir:
            names = {c: j["name"] for c, j in JURISDICTIONS.items()}
            m = write_downloads(args.data_dir, args.downloads_dir, names, now)
            print(f"downloads: {m['meetings']} meetings, {len(m['places'])} places",
                  file=sys.stderr)

    doc = assemble(hearings, codes, source, now)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
        print(f"wrote {len(doc['hearings'])} hearings "
              f"({', '.join(f'{k}:{v}' for k, v in doc['counts'].items()) or 'none'}) "
              f"to {args.output}", file=sys.stderr)

    if args.rss:
        Path(args.rss).write_text(to_rss(doc))
        print(f"wrote RSS feed to {args.rss}", file=sys.stderr)

    if args.places_dir and args.participation:
        n = write_place_pages(doc, args.participation, args.places_dir,
                              Path(args.places_dir).parent / "sitemap.xml", args.downloads_dir)
        print(f"wrote {n} place pages to {args.places_dir}", file=sys.stderr)

    if args.rss_feeds_dir:
        outdir = Path(args.rss_feeds_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        feeds = all_feeds(doc, args.participation)
        for fname, xml in feeds.items():
            (outdir / fname).write_text(xml)
        print(f"wrote {len(feeds)} granular RSS feeds to {outdir}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
