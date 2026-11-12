#!/usr/bin/env python3
"""13F Monitor — single-file edition.

Fetches recent 13F filings for the institutions in WATCHLIST,
stores them locally in SQLite, compares each one against the previous
quarter, and writes a self-contained HTML report.

Usage:
    python monitor.py                     # fetch latest + generate report
    python monitor.py --backfill 4        # also pull last 4 quarters per institution
    python monitor.py --quarter 2026Q3    # fetch and compare the specified quarter
    python monitor.py --no-fetch          # skip API calls, regenerate from DB
    python monitor.py --output report.html

Set SEC_USER_AGENT in your environment before fetching.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path

import requests

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit me
# ═══════════════════════════════════════════════════════════════════════

# SEC blocks requests without a real User-Agent. Put your real email.
USER_AGENT = os.environ.get("SEC_USER_AGENT", "")

# Institutions to monitor. (CIK, display name).
# Look up CIKs: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
WATCHLIST: list[tuple[int, str]] = [
    (1067983, "Berkshire Hathaway"),
    (1350694, "Bridgewater Associates"),
    (1536411, "Duquesne Family Office"),
    (1037389, "Renaissance Technologies"),
    (1179392, "Two Sigma Investments"),
    (1423053, "Citadel Advisors"),
    (2026053, "Pershing Square Inc."),
    (1656456, "Appaloosa Management"),
    (1167483, "Tiger Global Management"),
    (1061165, "Lone Pine Capital"),
    (1603466, "Point72 Asset Management"),
    (315066, "FMR LLC (富达)"),
]

# Optional Chinese names for holdings shown in the report.
# Key can be either:
#   1) CUSIP (recommended, most stable)
#   2) SEC issuer name as it appears in the filing
#
# Add to this list over time and the HTML report will render the Chinese
# name under the English issuer name automatically.
SECURITY_ZH_NAMES_RAW: dict[str, str] = {
    "APPLE INC": "苹果",
    "MICROSOFT CORP": "微软",
    "NVIDIA CORP": "英伟达",
    "AMAZON COM INC": "亚马逊",
    "ALPHABET INC": "谷歌",
    "META PLATFORMS INC": "Meta",
    "TESLA INC": "特斯拉",
    "BROADCOM INC": "博通",
    "NETFLIX INC": "奈飞",
    "ORACLE CORP": "甲骨文",
    "JPMORGAN CHASE & CO": "摩根大通",
    "BANK AMER CORP": "美国银行",
    "VISA INC": "Visa",
    "MASTERCARD INC": "万事达",
    "AMERICAN EXPRESS CO": "美国运通",
    "COCA COLA CO": "可口可乐",
    "CHEVRON CORP NEW": "雪佛龙",
    "OCCIDENTAL PETE CORP DEL": "西方石油",
    "COSTCO WHSL CORP NEW": "好市多",
    "WALMART INC": "沃尔玛",
    "PROCTER & GAMBLE CO": "宝洁",
    # Examples:
    # "037833100": "苹果",
    # "BERKSHIRE HATHAWAY INC DEL": "伯克希尔哈撒韦",
}

# Optional external Chinese-name mappings. Put more names here without
# editing Python code; file values override the built-ins above.
SECURITY_ZH_NAMES_PATH = Path("security_zh_names.json")
SECURITY_METADATA_PATH = Path("security_metadata.json")

# Thresholds
ADD_THRESHOLD = 0.5             # added >50%
REDUCE_THRESHOLD = -0.5         # reduced >50%
MIN_POSITION_VALUE = 1_000_000  # ignore positions <$1M
CROWD_MIN_INSTITUTIONS = 3      # crowd-buy: ≥3 funds
CROWD_MIN_TOTAL_VALUE = 100_000_000  # crowd-buy: aggregated >$100M
TOP_N_PER_BUCKET = 25           # cap items per table
MIN_13F_FILER_TOTAL = 100_000_000  # below this is suspicious for a 13F filer

# Paths
DB_PATH = Path(os.environ.get("SEC_13F_DB", "data/monitor.db"))
REPORT_DIR = Path("data")
REPORT_TIMEZONE = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")


# ───────────────────────────── EDGAR client ────────────────────────────
class EdgarClient:
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

    def __init__(self, user_agent: str):
        if "@" not in user_agent or user_agent.startswith("Your Name"):
            raise ValueError(
                "Set SEC_USER_AGENT to your name and contact email. "
                "SEC blocks generic User-Agents."
            )
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.session.headers["Accept-Encoding"] = "gzip, deflate"
        self._last = 0.0

    def _get(self, url: str) -> requests.Response:
        elapsed = time.time() - self._last
        if elapsed < 0.11:  # ~9 req/s — SEC limits 10/s
            time.sleep(0.11 - elapsed)
        self._last = time.time()
        host = url.split("/")[2]
        r = self.session.get(url, headers={"Host": host}, timeout=30)
        r.raise_for_status()
        return r

    def latest_n_13f(self, cik: int, n: int = 1,
                     report_dates: tuple[str, str] | None = None) -> list[dict]:
        """Return holdings filings for the selected distinct reporting periods."""
        r = self._get(self.SUBMISSIONS.format(cik=cik))
        recent = r.json().get("filings", {}).get("recent", {})
        filings = [
            {
                "accession": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": recent["reportDate"][i],
                "primary_document": recent["primaryDocument"][i],
                "form": form,
            }
            for i, form in enumerate(recent.get("form", []))
            if form in ("13F-HR", "13F-HR/A") and recent["reportDate"][i]
        ]
        if report_dates is None:
            periods = sorted({f["report_date"] for f in filings}, reverse=True)
            selected_periods = set(periods[:n])
        else:
            selected_periods = set(report_dates)
        return sorted(
            (f for f in filings if f["report_date"] in selected_periods),
            key=lambda f: (f["report_date"], f["filing_date"], f["accession"]),
            reverse=True,
        )

    def amendment_details(self, cik: int, filing: dict) -> tuple[str | None, int]:
        if filing["form"] != "13F-HR/A":
            return None, 0
        accession = filing["accession"].replace("-", "")
        cover_name = filing["primary_document"].rsplit("/", 1)[-1]
        cover = ET.fromstring(self._get(f"{self.ARCHIVES}/{cik}/{accession}/{cover_name}").content)
        kind = re.sub(r"[^A-Z]", "", cover.findtext(".//{*}amendmentType", "").upper())
        if kind not in {"RESTATEMENT", "NEWHOLDINGS"}:
            raise ValueError(f"Unknown amendment type for {filing['accession']}")
        return kind, int(cover.findtext(".//{*}amendmentNo", "0"))

    def fetch_holdings(self, cik: int, accession: str) -> list[dict]:
        """Download the 13F information table XML and parse it."""
        acc = accession.replace("-", "")
        idx = self._get(f"{self.ARCHIVES}/{cik}/{acc}/index.json").json()
        items = idx.get("directory", {}).get("item", [])

        xml_url = None
        for item in items:
            name = item["name"].lower()
            if name.endswith(".xml") and ("infotable" in name or "info_table" in name):
                xml_url = f"{self.ARCHIVES}/{cik}/{acc}/{item['name']}"
                break
        if not xml_url:
            for item in items:
                name = item["name"].lower()
                if name.endswith(".xml") and "primary_doc" not in name:
                    xml_url = f"{self.ARCHIVES}/{cik}/{acc}/{item['name']}"
                    break
        if not xml_url:
            return []
        return parse_information_table(self._get(xml_url).text)


# ───────────────────────────── XML parser ──────────────────────────────
def parse_information_table(xml_text: str) -> list[dict]:
    def strip_ns(t: str) -> str:
        return t.split("}", 1)[-1] if "}" in t else t

    root = ET.fromstring(xml_text)
    out = []
    for elem in root.iter():
        if strip_ns(elem.tag) != "infoTable":
            continue
        f: dict = {}
        for child in elem:
            tag = strip_ns(child.tag)
            if tag == "shrsOrPrnAmt":
                for sub in child:
                    if strip_ns(sub.tag) == "sshPrnamt":
                        f["shares"] = int(sub.text or 0)
                    elif strip_ns(sub.tag) == "sshPrnamtType":
                        f["share_type"] = (sub.text or "").strip()
            else:
                f[tag] = (child.text or "").strip() if child.text else ""

        try:
            value = int(float((f.get("value", "0") or "0").replace(",", "")))
        except ValueError:
            value = 0

        out.append({
            "name_of_issuer": f.get("nameOfIssuer", ""),
            "title_of_class": f.get("titleOfClass", ""),
            "cusip": (f.get("cusip", "") or "").upper(),
            "value": value,
            "shares": f.get("shares", 0),
            "share_type": f.get("share_type", ""),
            "put_call": f.get("putCall", ""),
        })

    # Some managers still populate XML market values in $000s. When the whole
    # filing totals well below the 13F reporting threshold, rescale to dollars.
    total_value = sum(h["value"] for h in out)
    if 0 < total_value < MIN_13F_FILER_TOTAL:
        for h in out:
            h["value"] *= 1000
    return out


# ───────────────────────────── Storage ────────────────────────────────
class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(db_path)
        with self._c() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS filings (
                    accession TEXT PRIMARY KEY,
                    cik INTEGER, form TEXT,
                    filing_date DATE, report_date DATE,
                    amendment_type TEXT, amendment_no INTEGER,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession TEXT, cik INTEGER, report_date DATE,
                    cusip TEXT, name_of_issuer TEXT, title_of_class TEXT,
                    value INTEGER, shares INTEGER, put_call TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_h ON holdings(cik, report_date);
                CREATE INDEX IF NOT EXISTS idx_c ON holdings(cusip, report_date);
            """)
            c.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in c.execute("PRAGMA table_info(filings)")}
            if "amendment_type" not in columns:
                c.execute("ALTER TABLE filings ADD COLUMN amendment_type TEXT")
            if "amendment_no" not in columns:
                c.execute("ALTER TABLE filings ADD COLUMN amendment_no INTEGER")

    @contextmanager
    def _c(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def is_empty(self) -> bool:
        with self._c() as c:
            return c.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0

    def repair_implausibly_small_filings(self) -> list[dict]:
        """Rescale filings whose total value is implausibly low for a 13F."""
        with self._c() as c:
            rows = c.execute("""
                SELECT f.accession, f.cik, f.report_date, SUM(h.value) AS total_value
                FROM filings f
                JOIN holdings h ON h.accession = f.accession
                GROUP BY f.accession, f.cik, f.report_date
                HAVING total_value > 0 AND total_value < ?
            """, (MIN_13F_FILER_TOTAL,)).fetchall()
            repaired = [dict(r) for r in rows]
            for r in repaired:
                c.execute(
                    "UPDATE holdings SET value = value * 1000 WHERE accession=?",
                    (r["accession"],),
                )
            return repaired

    def has(self, accession: str) -> bool:
        with self._c() as c:
            return c.execute(
                "SELECT 1 FROM filings WHERE accession=? "
                "AND (form!='13F-HR/A' OR amendment_type IS NOT NULL)",
                (accession,),
            ).fetchone() is not None

    def save(self, accession, cik, form, filing_date, report_date, holdings,
             amendment_type=None, amendment_no=0):
        with self._c() as c:
            c.execute(
                "INSERT OR REPLACE INTO filings "
                "(accession,cik,form,filing_date,report_date,amendment_type,amendment_no) "
                "VALUES (?,?,?,?,?,?,?)",
                (accession, cik, form, filing_date, report_date, amendment_type, amendment_no),
            )
            c.execute("DELETE FROM holdings WHERE accession=?", (accession,))
            c.executemany(
                "INSERT INTO holdings (accession,cik,report_date,cusip,name_of_issuer,"
                "title_of_class,value,shares,put_call) VALUES (?,?,?,?,?,?,?,?,?)",
                [(accession, cik, report_date, h["cusip"], h["name_of_issuer"],
                  h["title_of_class"], h["value"], h["shares"], h["put_call"])
                 for h in holdings if h["cusip"]],
            )

    def latest_filing(self, cik: int, report_date: str | None = None):
        period_filter = "AND report_date=?" if report_date else ""
        parameters = (cik, report_date) if report_date else (cik,)
        with self._c() as c:
            r = c.execute(
                "SELECT accession, filing_date, report_date FROM filings "
                f"WHERE cik=? {period_filter} "
                "ORDER BY report_date DESC, filing_date DESC, amendment_no DESC, accession DESC LIMIT 1",
                parameters,
            ).fetchone()
            return dict(r) if r else None

    def prev_report_date(self, cik: int, before: str):
        with self._c() as c:
            r = c.execute(
                "SELECT MAX(report_date) FROM holdings WHERE cik=? AND report_date<?",
                (cik, before),
            ).fetchone()
            return r[0] if r and r[0] else None

    def _effective_accessions(self, conn, cik: int, report_date: str) -> list[str]:
        rows = conn.execute("""
            SELECT accession, form, amendment_type FROM filings
            WHERE cik=? AND report_date=?
            ORDER BY filing_date, COALESCE(amendment_no, 0), accession
        """, (cik, report_date))
        accessions = []
        for row in rows:
            if row["form"] == "13F-HR" or row["amendment_type"] == "RESTATEMENT":
                accessions = [row["accession"]]
            elif row["amendment_type"] == "NEWHOLDINGS" and accessions:
                accessions.append(row["accession"])
        return accessions

    def holdings_for(self, cik: int, report_date: str) -> list[dict]:
        """Return aggregated long-equity holdings (excludes options)."""
        with self._c() as c:
            accessions = self._effective_accessions(c, cik, report_date)
            if not accessions:
                return []
            placeholders = ",".join("?" * len(accessions))
            rows = c.execute(f"""
                SELECT cusip,
                       MAX(name_of_issuer) AS name_of_issuer,
                       SUM(value) AS value,
                       SUM(shares) AS shares
                FROM holdings
                WHERE cik=? AND report_date=? AND accession IN ({placeholders})
                  AND (put_call='' OR put_call IS NULL)
                GROUP BY cusip
            """, (cik, report_date, *accessions)).fetchall()
            return [dict(r) for r in rows]

    def latest_report_date(self):
        with self._c() as c:
            r = c.execute("SELECT MAX(report_date) FROM holdings").fetchone()
            return r[0] if r and r[0] else None

    def detect_crowd_buys(self, report_date: str, previous_report_date: str | None = None,
                          comparable_ciks: set[int] | None = None) -> list[dict]:
        """Find stocks newly bought (no prior position) by ≥N watchlist funds
        in the same quarter."""
        cik_names = {cik: name for cik, name in WATCHLIST}
        with self._c() as c:
            ciks = [cik for cik in cik_names if comparable_ciks is None or cik in comparable_ciks]
            if len(ciks) < CROWD_MIN_INSTITUTIONS:
                return []
            current = [a for cik in ciks for a in self._effective_accessions(c, cik, report_date)]
            if not current:
                return []
            current_slots = ",".join("?" * len(current))
            if previous_report_date:
                previous = [a for cik in ciks for a in self._effective_accessions(c, cik, previous_report_date)]
                if not previous:
                    return []
                previous_slots = ",".join("?" * len(previous))
                previous_filter = f"h2.report_date=? AND h2.accession IN ({previous_slots})"
                previous_values = [previous_report_date, *previous]
            else:
                previous_filter = "h2.report_date<?"
                previous_values = [report_date]
            rows = c.execute(f"""
                SELECT h1.cusip,
                       MAX(h1.name_of_issuer) AS name_of_issuer,
                       COUNT(DISTINCT h1.cik) AS inst_count,
                       SUM(h1.value) AS total_value,
                       GROUP_CONCAT(DISTINCT h1.cik) AS ciks
                FROM holdings h1
                WHERE h1.report_date=? AND h1.accession IN ({current_slots})
                  AND (h1.put_call='' OR h1.put_call IS NULL)
                  AND NOT EXISTS (
                      SELECT 1 FROM holdings h2
                      WHERE h2.cik=h1.cik AND h2.cusip=h1.cusip
                        AND (h2.put_call='' OR h2.put_call IS NULL)
                        AND {previous_filter}
                  )
                GROUP BY h1.cusip
                HAVING inst_count >= ? AND total_value >= ?
                ORDER BY inst_count DESC, total_value DESC
                LIMIT ?
            """, (report_date, *current, *previous_values,
                  CROWD_MIN_INSTITUTIONS, CROWD_MIN_TOTAL_VALUE,
                  TOP_N_PER_BUCKET)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                ciks = [int(x) for x in (d.pop("ciks") or "").split(",") if x]
                d["institutions"] = [cik_names.get(c, f"CIK {c}") for c in ciks]
                out.append(d)
            return out


# ───────────────────────────── Analyzer ───────────────────────────────
def diff_holdings(curr: list[dict], prev: list[dict]) -> dict:
    cm = {h["cusip"]: h for h in curr if h["cusip"]}
    pm = {h["cusip"]: h for h in prev if h["cusip"]}
    new, sold, added, reduced = [], [], [], []

    for cu, h in cm.items():
        if (h["value"] or 0) < MIN_POSITION_VALUE:
            continue
        if cu not in pm:
            new.append(h)
            continue
        ps = pm[cu]["shares"] or 0
        cs = h["shares"] or 0
        if ps <= 0:
            continue
        change = (cs - ps) / ps
        if change >= ADD_THRESHOLD:
            added.append({**h, "change_pct": change, "prev_shares": ps})
        elif change <= REDUCE_THRESHOLD:
            reduced.append({**h, "change_pct": change, "prev_shares": ps})

    for cu, h in pm.items():
        if cu not in cm and (h["value"] or 0) >= MIN_POSITION_VALUE:
            sold.append(h)

    return {
        "new_positions": sorted(new, key=lambda x: x["value"], reverse=True)[:TOP_N_PER_BUCKET],
        "sold_out": sorted(sold, key=lambda x: x["value"], reverse=True)[:TOP_N_PER_BUCKET],
        "added": sorted(added, key=lambda x: x["value"], reverse=True)[:TOP_N_PER_BUCKET],
        "reduced": sorted(reduced, key=lambda x: x["value"], reverse=True)[:TOP_N_PER_BUCKET],
    }


# ───────────────────────────── HTML render ────────────────────────────
def fmt_usd(v: int) -> str:
    v = v or 0
    if v >= 1_000_000_000: return f"${v/1e9:.2f}B"
    if v >= 1_000_000:     return f"${v/1e6:.1f}M"
    if v >= 1_000:         return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def fmt_pct(p: float) -> str:
    return f"+{p:.0%}" if p >= 0 else f"{p:.0%}"


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


NAME_TOKEN_ALIASES = {
    "CORP": "CORPORATION",
    "CORPORATION": "CORPORATION",
    "INC": "INCORPORATED",
    "INCORPORATED": "INCORPORATED",
    "CO": "COMPANY",
    "COMPANY": "COMPANY",
    "COS": "COMPANIES",
    "COMPANIES": "COMPANIES",
    "HLDG": "HOLDINGS",
    "HLDGS": "HOLDINGS",
    "HOLD": "HOLDINGS",
    "HOLDI": "HOLDINGS",
    "HOLDINGS": "HOLDINGS",
    "TECH": "TECHNOLOGY",
    "TECHNOLOGIES": "TECHNOLOGIES",
    "TECHNOLOGY": "TECHNOLOGY",
    "INTL": "INTERNATIONAL",
    "INTERNATIONAL": "INTERNATIONAL",
    "FINL": "FINANCIAL",
    "FINANCIAL": "FINANCIAL",
    "MTR": "MOTOR",
    "MTRS": "MOTORS",
    "MOTOR": "MOTOR",
    "MOTORS": "MOTORS",
    "MFG": "MANUFACTURING",
    "MANUFAC": "MANUFACTURING",
    "MANUFACTURING": "MANUFACTURING",
    "SVCS": "SERVICES",
    "SERVICE": "SERVICES",
    "SERVICES": "SERVICES",
    "SYS": "SYSTEMS",
    "SYSTEM": "SYSTEMS",
    "SYSTEMS": "SYSTEMS",
    "LAB": "LABORATORIES",
    "LABS": "LABORATORIES",
    "LABORATORY": "LABORATORIES",
    "LABORATORIES": "LABORATORIES",
    "BK": "BANK",
    "BANC": "BANK",
    "BANK": "BANK",
    "FD": "FUND",
    "FDS": "FUNDS",
    "FUND": "FUND",
    "FUNDS": "FUNDS",
    "TR": "TRUST",
    "TRUST": "TRUST",
    "WHSL": "WHOLESALE",
    "AMER": "AMERICA",
    "AMERN": "AMERICAN",
    "MGMT": "MANAGEMENT",
    "MNG": "MINING",
    "INDS": "INDUSTRIES",
    "IND": "INDUSTRIES",
    "MATLS": "MATERIALS",
    "&": "AND",
}

TRAILING_NAME_NOISE = {
    "INCORPORATED",
    "CORPORATION",
    "COMPANY",
    "COMPANIES",
    "GROUP",
    "HOLDINGS",
    "PLC",
    "LIMITED",
    "LTD",
    "TRUST",
    "FUND",
    "FUNDS",
    "NEW",
    "DELAWARE",
    "SA",
    "NV",
    "US",
    "CDA",
}


def normalize_security_key(value: str) -> str:
    text = unescape(value or "").upper().replace(".", " ")
    tokens = re.findall(r"[A-Z0-9]+|&", text)
    normalized = [NAME_TOKEN_ALIASES.get(token, token) for token in tokens]
    return " ".join(normalized)


def security_key_candidates(value: str) -> list[str]:
    base = normalize_security_key(value)
    if not base:
        return []

    candidates = [base]
    tokens = base.split()
    while tokens and tokens[-1] in TRAILING_NAME_NOISE:
        tokens = tokens[:-1]
        if tokens:
            candidates.append(" ".join(tokens))
    return candidates


def load_security_zh_names_raw() -> dict[str, str]:
    mapping = dict(SECURITY_ZH_NAMES_RAW)
    if not SECURITY_ZH_NAMES_PATH.exists():
        return mapping

    try:
        extra = json.loads(SECURITY_ZH_NAMES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to load %s: %s", SECURITY_ZH_NAMES_PATH, e)
        return mapping

    if not isinstance(extra, dict):
        log.warning("%s must contain a JSON object mapping name -> zh name", SECURITY_ZH_NAMES_PATH)
        return mapping

    for key, value in extra.items():
        if key and value:
            mapping[str(key)] = str(value)
    return mapping


SECURITY_ZH_NAMES: dict[str, str] = {}
for _key, _value in load_security_zh_names_raw().items():
    for _candidate in security_key_candidates(_key):
        SECURITY_ZH_NAMES.setdefault(_candidate, _value)


def load_security_metadata_raw() -> dict[str, dict]:
    if not SECURITY_METADATA_PATH.exists():
        return {}

    try:
        raw = json.loads(SECURITY_METADATA_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to load %s: %s", SECURITY_METADATA_PATH, e)
        return {}

    if not isinstance(raw, dict):
        log.warning("%s must contain a JSON object mapping key -> metadata", SECURITY_METADATA_PATH)
        return {}

    mapping: dict[str, dict] = {}
    for key, value in raw.items():
        if not key or not isinstance(value, dict):
            continue
        entry = {
            "sector": str(value.get("sector", "") or "").strip(),
            "business": str(value.get("business", "") or "").strip(),
            "source": str(value.get("source", "") or "").strip(),
        }
        if entry["sector"] or entry["business"]:
            mapping[str(key)] = entry
    return mapping


SECURITY_METADATA: dict[str, dict] = {}
for _key, _value in load_security_metadata_raw().items():
    if re.fullmatch(r"[A-Z0-9]{8,10}", _key):
        SECURITY_METADATA[_key] = _value
    for _candidate in security_key_candidates(_key):
        SECURITY_METADATA.setdefault(_candidate, _value)


def lookup_security_zh_name(row: dict) -> str:
    for key in (row.get("cusip", ""), row.get("name_of_issuer", "")):
        for candidate in security_key_candidates(key):
            zh_name = SECURITY_ZH_NAMES.get(candidate)
            if zh_name:
                return zh_name
    return ""


def lookup_security_metadata(row: dict) -> dict:
    cusip = (row.get("cusip", "") or "").upper().strip()
    if cusip:
        meta = SECURITY_METADATA.get(cusip)
        if meta:
            return meta

    for candidate in security_key_candidates(row.get("name_of_issuer", "")):
        meta = SECURITY_METADATA.get(candidate)
        if meta:
            return meta
    return {}


def render_security_name(row: dict) -> str:
    en_name = escape(row.get("name_of_issuer", ""))
    zh_name = lookup_security_zh_name(row)
    meta = lookup_security_metadata(row)

    name_line = f'<span class="security-en">{en_name}</span>'
    if zh_name:
        name_line += f'<span class="security-zh">（{escape(zh_name)}）</span>'

    meta_bits = []
    if meta.get("sector"):
        meta_bits.append(f'板块：{escape(meta["sector"])}')
    if meta.get("business"):
        meta_bits.append(f'主营：{escape(meta["business"])}')

    if not meta_bits:
        return name_line if zh_name else en_name

    return (
        '<div class="security-name">'
        f'<div class="security-line">{name_line}</div>'
        f'<div class="security-meta">{" · ".join(meta_bits)}</div>'
        '</div>'
    )


HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>13F 持仓监控 — {date}</title>
<style>
:root {{
  --bg: #f5f6f8;
  --card: #ffffff;
  --border: #dce2ea;
  --border-soft: #eef1f5;
  --text: #17202a;
  --muted: #657386;
  --green: #087a55; --green-bg: #e8f6ef;
  --red: #b42318;   --red-bg: #fff0ee;
  --amber: #8a5a00; --amber-bg: #fff6df;
  --blue: #2457a6;  --blue-bg: #edf4ff;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #11161d;
    --card: #18202a;
    --border: #2a3543;
    --border-soft: #222c38;
    --text: #edf2f7;
    --muted: #9aa8b8;
    --green: #63d6ad; --green-bg: rgba(8,122,85,0.18);
    --red: #ff9b92;   --red-bg: rgba(180,35,24,0.18);
    --amber: #ffc45c; --amber-bg: rgba(138,90,0,0.20);
    --blue: #8fb7ff;  --blue-bg: rgba(36,87,166,0.22);
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 18px 20px;
  line-height: 1.42; font-size: 13px;
}}
.container {{ max-width: 1280px; margin: 0 auto; }}
header {{
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: 16px; margin-bottom: 12px; padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}}
h1 {{ font-size: 22px; font-weight: 650; margin: 0; letter-spacing: 0; }}
.subtitle {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
.summary {{
  display: grid;
  grid-template-columns: repeat(4, minmax(130px, 1fr));
  margin: 0 0 12px; overflow: hidden;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
}}
.stat {{
  padding: 9px 12px; border-right: 1px solid var(--border-soft);
}}
.stat:last-child {{ border-right: none; }}
.stat-label {{
  color: var(--muted); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px;
}}
.stat-value {{ font-size: 22px; line-height: 1.05; font-weight: 650; font-variant-numeric: tabular-nums; }}
nav {{
  position: sticky; top: 0; z-index: 5;
  background: color-mix(in srgb, var(--card) 94%, transparent);
  backdrop-filter: blur(8px);
  border: 1px solid var(--border);
  border-radius: 6px; padding: 7px 10px;
  margin-bottom: 12px; font-size: 12px; line-height: 1.9;
}}
nav strong {{ font-weight: 650; margin-right: 7px; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; }}
nav a {{
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--text); text-decoration: none; margin: 0 4px 3px 0;
  padding: 1px 7px; border: 1px solid var(--border-soft); border-radius: 4px;
  background: var(--card);
}}
nav a:hover {{ border-color: var(--blue); color: var(--blue); }}
nav .pill {{
  display: inline-block; padding: 0 5px;
  background: var(--amber-bg); color: var(--amber);
  border-radius: 999px; font-size: 10px; font-weight: 650;
}}
.section {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 13px 14px;
  margin-bottom: 12px; scroll-margin-top: 64px;
}}
.section-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 8px;
  margin-bottom: 7px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
}}
.section-title {{ font-size: 16px; font-weight: 650; }}
.section-meta {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
.bucket {{ margin-top: 10px; }}
.bucket-title {{
  font-size: 11px; font-weight: 650;
  text-transform: uppercase; letter-spacing: 0.03em;
  padding: 3px 8px; border-radius: 4px;
  display: inline-block; margin-bottom: 5px;
}}
.bucket-new, .bucket-add {{ color: var(--green); background: var(--green-bg); }}
.bucket-sold, .bucket-reduce {{ color: var(--red); background: var(--red-bg); }}
.bucket-crowd {{ color: var(--amber); background: var(--amber-bg); }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--border-soft); border-radius: 5px; }}
table {{ width: 100%; min-width: 760px; border-collapse: collapse; font-size: 12px; line-height: 1.28; }}
th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }}
th {{
  color: var(--muted); font-weight: 650; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.04em;
  background: color-mix(in srgb, var(--bg) 72%, var(--card));
}}
tbody tr:nth-child(even) {{ background: color-mix(in srgb, var(--bg) 42%, transparent); }}
tbody tr:hover {{ background: var(--blue-bg); }}
tr:last-child td {{ border-bottom: none; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.empty {{ color: var(--muted); font-size: 12px; padding: 6px 0; font-style: italic; }}
.pct-up {{ color: var(--green); font-weight: 650; font-variant-numeric: tabular-nums; }}
.pct-down {{ color: var(--red); font-weight: 650; font-variant-numeric: tabular-nums; }}
.institutions {{ color: var(--muted); font-size: 11px; max-width: 360px; }}
.cusip {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; color: var(--muted); }}
.security-name {{ display: flex; flex-direction: column; gap: 1px; min-width: 0; }}
.security-line {{ min-width: 0; }}
.security-en {{ font-weight: 600; }}
.security-zh {{ color: var(--muted); font-size: 11px; margin-left: 5px; white-space: nowrap; }}
.security-meta {{ color: var(--muted); font-size: 11px; line-height: 1.22; }}
.no-change-list {{ color: var(--muted); font-size: 12px; line-height: 1.65; }}
.no-change-list span {{ display: inline-block; padding: 2px 7px; margin: 0 3px 4px 0;
  border: 1px solid var(--border-soft); border-radius: 4px; background: var(--card); }}
footer {{
  margin-top: 18px; padding-top: 12px;
  color: var(--muted); font-size: 11px; text-align: center;
  border-top: 1px solid var(--border);
}}
footer a {{ color: var(--muted); }}
@media (max-width: 640px) {{
  body {{ padding: 12px 10px; }}
  header {{ display: block; }}
  .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .stat:nth-child(2) {{ border-right: none; }}
  .stat:nth-child(-n+2) {{ border-bottom: 1px solid var(--border-soft); }}
  .section {{ padding: 11px 10px; }}
  th, td {{ padding: 4px 6px; }}
  .stat-value {{ font-size: 20px; }}
  .institutions {{ max-width: 180px; overflow: hidden;
    white-space: nowrap; text-overflow: ellipsis; }}
}}
</style>
</head>
<body>
<div class="container">
<header>
<h1>13F 持仓监控报告</h1>
<div class="subtitle">{time}</div>
</header>
"""

HTML_FOOT = """<footer>
数据来源 <a href="https://www.sec.gov/edgar/searchedgar/companysearch">SEC EDGAR</a> ·
13F 披露存在 45 天延迟 ·
仅含美股多头持仓（不含做空、未做期权净轧）
</footer>
</div>
</body>
</html>
"""


def render_bucket(title: str, css: str, rows: list[dict], cols: list[tuple]) -> str:
    if not rows:
        return ""
    out = [f'<div class="bucket"><span class="bucket-title bucket-{css}">{escape(title)} ({len(rows)})</span>']
    out.append('<div class="table-wrap"><table><thead><tr>')
    for label, _, klass in cols:
        out.append(f'<th class="{klass}">{escape(label)}</th>')
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for _, fn, klass in cols:
            out.append(f'<td class="{klass}">{fn(r)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div></div>")
    return "".join(out)


def render_diff(diff: dict) -> str:
    parts = []

    parts.append(render_bucket(
        "新建仓", "new", diff["new_positions"],
        [
            ("标的", render_security_name, ""),
            ("CUSIP", lambda x: f'<span class="cusip">{escape(x["cusip"])}</span>', ""),
            ("市值", lambda x: fmt_usd(x["value"]), "num"),
            ("股数", lambda x: f"{x['shares']:,}", "num"),
        ],
    ))
    parts.append(render_bucket(
        "清仓", "sold", diff["sold_out"],
        [
            ("标的", render_security_name, ""),
            ("CUSIP", lambda x: f'<span class="cusip">{escape(x["cusip"])}</span>', ""),
            ("原市值", lambda x: fmt_usd(x["value"]), "num"),
            ("原股数", lambda x: f"{x['shares']:,}", "num"),
        ],
    ))
    parts.append(render_bucket(
        "加仓 >50%", "add", diff["added"],
        [
            ("标的", render_security_name, ""),
            ("CUSIP", lambda x: f'<span class="cusip">{escape(x["cusip"])}</span>', ""),
            ("市值", lambda x: fmt_usd(x["value"]), "num"),
            ("变化", lambda x: f'<span class="pct-up">{fmt_pct(x["change_pct"])}</span>', "num"),
        ],
    ))
    parts.append(render_bucket(
        "减仓 >50%", "reduce", diff["reduced"],
        [
            ("标的", render_security_name, ""),
            ("CUSIP", lambda x: f'<span class="cusip">{escape(x["cusip"])}</span>', ""),
            ("市值", lambda x: fmt_usd(x["value"]), "num"),
            ("变化", lambda x: f'<span class="pct-down">{fmt_pct(x["change_pct"])}</span>', "num"),
        ],
    ))
    return "".join(parts)


def render_report(report: dict) -> str:
    parts = [HTML_HEAD.format(
        date=report["generated_at"][:10],
        time=f"生成于 {report['generated_at']}",
    )]

    # Stats
    parts.append('<div class="summary">')
    for s in report["stats"]:
        parts.append(
            f'<div class="stat">'
            f'<div class="stat-label">{escape(s["label"])}</div>'
            f'<div class="stat-value">{escape(str(s["value"]))}</div>'
            f'</div>'
        )
    parts.append('</div>')

    if report.get("target_report_date"):
        status_labels = {
            "ok": "已获取，可对比",
            "no_change": "已获取，可对比",
            "first_filing": "已获取，缺少上季数据",
            "not_filed": "暂未披露",
            "cache_missing": "本地暂无该季数据",
            "fetch_failed": "获取失败，请查看终端日志",
        }
        parts.append('<section class="section">')
        parts.append(render_bucket(
            f'披露进度 · {report["target_report_date"]}', "crowd", report["institutions"],
            [
                ("机构", lambda row: escape(row["name"]), ""),
                ("状态", lambda row: escape(status_labels[row["status"]]), ""),
                ("最近可用报告期", lambda row: escape(row.get("report_date") or "—"), ""),
                ("最近可用申报日", lambda row: escape(row.get("filing_date") or "—"), ""),
            ],
        ))
        parts.append('</section>')

    # TOC
    insts_with_changes = [i for i in report["institutions"] if i["status"] == "ok"]
    insts_no_change = [i for i in report["institutions"] if i["status"] == "no_change"]
    insts_first = [i for i in report["institutions"] if i["status"] == "first_filing"]

    if report["crowd_buys"] or insts_with_changes:
        parts.append('<nav><strong>跳转至：</strong>')
        if report["crowd_buys"]:
            parts.append(f' <a href="#crowd">集中扫货 <span class="pill">{len(report["crowd_buys"])}</span></a>')
        for inst in insts_with_changes:
            slug = slugify(inst["name"])
            parts.append(f' <a href="#{slug}">{escape(inst["name"])}</a>')
        parts.append('</nav>')

    # Crowd buys
    if report["crowd_buys"]:
        parts.append('<section id="crowd" class="section">')
        parts.append(
            '<div class="section-header">'
            '<div class="section-title">集中扫货</div>'
            f'<div class="section-meta">{escape(report["crowd_buys_subtitle"])}</div>'
            '</div>'
        )
        parts.append(render_bucket(
            "多家机构同期新建仓", "crowd", report["crowd_buys"],
            [
                ("标的", render_security_name, ""),
                ("CUSIP", lambda x: f'<span class="cusip">{escape(x["cusip"])}</span>', ""),
                ("机构数", lambda x: str(x["inst_count"]), "num"),
                ("合计市值", lambda x: fmt_usd(x["total_value"]), "num"),
                ("关联机构", lambda x: f'<span class="institutions">{escape(", ".join(x["institutions"]))}</span>', ""),
            ],
        ))
        parts.append('</section>')

    # Institutions with material changes
    for inst in insts_with_changes:
        slug = slugify(inst["name"])
        parts.append(f'<section id="{slug}" class="section">')
        parts.append(
            '<div class="section-header">'
            f'<div class="section-title">{escape(inst["name"])}</div>'
            f'<div class="section-meta">报告期 {inst["report_date"]} · {inst["holdings_count"]} 个持仓 · {fmt_usd(inst["total_value"])}'
            f' · 对比 {inst.get("previous_report_date", "—")}</div>'
            '</div>'
        )
        parts.append(render_diff(inst["diff"]))
        parts.append('</section>')

    # No-change roundup
    if not report.get("target_report_date") and (insts_no_change or insts_first):
        parts.append('<section class="section">')
        parts.append('<div class="section-header"><div class="section-title">无重大变化</div></div>')
        parts.append('<div class="no-change-list">')
        for inst in insts_no_change:
            parts.append(
                f'<span>{escape(inst["name"])} '
                f'<small style="opacity:0.6">· {inst["report_date"]}</small></span>'
            )
        for inst in insts_first:
            parts.append(
                f'<span>{escape(inst["name"])} '
                f'<small style="opacity:0.6">· 首次记录</small></span>'
            )
        parts.append('</div></section>')

    parts.append(HTML_FOOT)
    return "".join(parts)


# ─────────────────────────────── Main ─────────────────────────────────
def quarter_dates(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"([1-9][0-9]{3})Q([1-4])", value.upper())
    if not match:
        raise argparse.ArgumentTypeError("Use YYYYQ1, YYYYQ2, YYYYQ3, or YYYYQ4")
    year, quarter = map(int, match.groups())
    ends = ("03-31", "06-30", "09-30", "12-31")
    current = f"{year}-{ends[quarter - 1]}"
    previous = f"{year - 1}-12-31" if quarter == 1 else f"{year}-{ends[quarter - 2]}"
    return current, previous


def main():
    ap = argparse.ArgumentParser(description="13F Monitor — single-file edition")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--backfill", type=int, default=0,
                       help="Pull last N reporting quarters per institution")
    scope.add_argument("--quarter", type=quarter_dates,
                       help="Fetch and compare a specified quarter, e.g. 2026Q3")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip API calls, just regenerate report from existing DB")
    ap.add_argument("--output", default=None, help="Output HTML path")
    ap.add_argument("--strict", action="store_true", help="Abort publication on SEC fetch errors")
    args = ap.parse_args()

    storage = Storage(DB_PATH)
    target_report_date, baseline_report_date = args.quarter or (None, None)

    # Decide how many quarters to fetch per institution
    if args.no_fetch:
        n_quarters = 0
    elif args.quarter:
        n_quarters = 2
    elif args.backfill:
        n_quarters = args.backfill
    elif storage.is_empty():
        log.info("DB is empty — auto-backfilling last 2 quarters per institution")
        n_quarters = 2
    else:
        n_quarters = 1

    # Phase 1: fetch and store
    new_filings = 0
    failed_ciks: set[int] = set()
    if n_quarters > 0:
        edgar = EdgarClient(USER_AGENT)
        log.info("Checking %d institutions (up to %d quarters each)...",
                 len(WATCHLIST), n_quarters)
        for cik, name in WATCHLIST:
            try:
                filings = edgar.latest_n_13f(cik, n_quarters, args.quarter)
                if not filings:
                    log.info("[%s] no 13F on file", name)
                    continue
                for f in filings:
                    if storage.has(f["accession"]):
                        continue
                    log.info("[%s] new: %s (report %s)",
                             name, f["accession"], f["report_date"])
                    amendment_type, amendment_no = edgar.amendment_details(cik, f)
                    holdings = edgar.fetch_holdings(cik, f["accession"])
                    if not holdings:
                        raise ValueError(f"Could not parse holdings for {f['accession']}")
                    storage.save(f["accession"], cik, f["form"],
                                 f["filing_date"], f["report_date"], holdings,
                                 amendment_type, amendment_no)
                    new_filings += 1
            except Exception as e:
                failed_ciks.add(cik)
                log.error("[%s] failed: %s", name, e)
        log.info("Stored %d filings; %d institutions had fetch errors", new_filings, len(failed_ciks))

    if args.strict and failed_ciks:
        ap.exit(1, "SEC fetch failed; report was not written.\n")

    # Phase 2: build report
    repaired = storage.repair_implausibly_small_filings()
    if repaired:
        cik_names = {cik: name for cik, name in WATCHLIST}
        for item in repaired:
            log.info("[%s] rescaled filing values by 1000 for %s (report %s)",
                     cik_names.get(item["cik"], f"CIK {item['cik']}"),
                     item["accession"], item["report_date"])

    log.info("Building report...")
    institutions_data = []
    total_changes = 0

    for cik, name in WATCHLIST:
        latest = storage.latest_filing(cik, target_report_date)
        if target_report_date and (not latest or cik in failed_ciks):
            cached = storage.latest_filing(cik) or {}
            status = "cache_missing" if args.no_fetch else "not_filed"
            if cik in failed_ciks:
                status = "fetch_failed"
            institutions_data.append({"name": name, "cik": cik, "status": status,
                                      "report_date": cached.get("report_date"),
                                      "filing_date": cached.get("filing_date")})
            continue
        if not latest:
            institutions_data.append({
                "name": name, "cik": cik, "status": "first_filing",
                "report_date": None, "holdings_count": 0, "total_value": 0,
            })
            continue

        current = storage.holdings_for(cik, latest["report_date"])
        prev_date = baseline_report_date if args.quarter else storage.prev_report_date(cik, latest["report_date"])

        inst = {
            "name": name, "cik": cik,
            "report_date": latest["report_date"],
            "filing_date": latest["filing_date"],
            "holdings_count": len(current),
            "total_value": sum(h["value"] or 0 for h in current),
        }

        previous = storage.holdings_for(cik, prev_date) if prev_date else []
        if target_report_date and not current:
            inst["status"] = "fetch_failed"
        elif previous:
            diff = diff_holdings(current, previous)
            inst["diff"] = diff
            inst["previous_report_date"] = prev_date
            has_changes = any([diff["new_positions"], diff["sold_out"],
                               diff["added"], diff["reduced"]])
            inst["status"] = "ok" if has_changes else "no_change"
            if has_changes:
                total_changes += (len(diff["new_positions"]) + len(diff["sold_out"]) +
                                  len(diff["added"]) + len(diff["reduced"]))
        else:
            inst["status"] = "first_filing"

        institutions_data.append(inst)

    # Sort: changed first (by most recent report_date), then no_change, then first_filing
    status_order = {"ok": 0, "no_change": 1, "first_filing": 2,
                    "not_filed": 3, "cache_missing": 3, "fetch_failed": 4}
    institutions_data.sort(
        key=lambda x: (status_order[x["status"]],
                       -(int(x["report_date"].replace("-", "")) if x["report_date"] else 0),
                       x["name"])
    )

    # Crowd buys
    latest_rd = target_report_date or max(
        (inst["report_date"] for inst in institutions_data if inst.get("report_date")),
        default=None,
    )
    comparable_ciks = {i["cik"] for i in institutions_data if i.get("previous_report_date")} if args.quarter else None
    crowd_buys = storage.detect_crowd_buys(latest_rd, baseline_report_date, comparable_ciks) if latest_rd else []
    crowd_subtitle = (
        f"≥{CROWD_MIN_INSTITUTIONS} 家机构在 {latest_rd} 同季新建仓的标的"
        if latest_rd else ""
    )

    latest_quarter_count = sum(
        1 for inst in institutions_data
        if latest_rd and inst.get("report_date") == latest_rd and inst["status"] in {"ok", "no_change", "first_filing"}
    )
    stats = [
        {"label": "监控机构", "value": len(WATCHLIST)},
        {"label": "目标季度已获取" if args.quarter else "最新季度机构", "value": latest_quarter_count},
        {"label": "重大变化", "value": total_changes},
        {"label": "集中扫货信号", "value": len(crowd_buys)},
    ]

    generated_at = datetime.now(REPORT_TIMEZONE)
    report_data = {
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M 北京时间"),
        "stats": stats,
        "institutions": institutions_data,
        "crowd_buys": crowd_buys,
        "crowd_buys_subtitle": crowd_subtitle,
        "target_report_date": target_report_date,
    }

    # Write
    if args.output:
        out_path = Path(args.output)
    elif args.quarter:
        out_path = REPORT_DIR / f"report-{target_report_date[:4]}Q{int(target_report_date[5:7]) // 3}.html"
    else:
        out_path = REPORT_DIR / f"report-{generated_at:%Y-%m-%d}.html"

    html = render_report(report_data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    log.info("Report written: %s (%d KB)", out_path, len(html) // 1024)
    print(f"\n📄 报告已生成：file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
