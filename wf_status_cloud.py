import asyncio
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin
import requests
from playwright.async_api import async_playwright

SERVER_NAME = "Get ReKt420"
TARGET_URL = "https://wfservers.com"
# Locked directly to Get ReKt 420*™ Midwest so we skip loading the homepage every run.
# Change this URL if you want to track a different wfservers.com server page.
SERVER_PAGE_URL = "https://www.wfservers.com/server.html?id=309494"
MAX_PLAYERS_SHOWN = 24

# Speed & reliability tuning
CACHE_TTL_SECONDS = 1800          # Re-use cached Get ReKt420 totals for 30 mins (ticks up with live session)
CACHE_MAX_AGE_SECONDS = 86400 * 30  # Keep seen players cached for 30 days
PROFILE_CONCURRENCY = 5           # Fetch up to 5 player profiles at once in Playwright
ENRICH_TIME_BUDGET_SECONDS = 45   # Never let profile lookups delay or time out the Discord update

THUMBNAIL_URL = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/228380/header.jpg"
ROSTER_IMAGE_FILENAME = "roster.png"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_state_path(filename: str) -> str:
    candidates = [
        os.path.join(BASE_DIR, filename),
        os.path.join(BASE_DIR, "UPLOAD_INSIDE_TO_GITHUB", filename),
        os.path.join(os.path.dirname(BASE_DIR), filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def is_valid_webhook(url: str) -> bool:
    url = (url or "").strip()
    return (
        url.startswith("https://discord.com/api/webhooks/")
        or url.startswith("https://discordapp.com/api/webhooks/")
        or url.startswith("https://ptb.discord.com/api/webhooks/")
        or url.startswith("https://canary.discord.com/api/webhooks/")
    )


def load_message_id():
    path = resolve_state_path("message_id.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            val = f.read().strip()
            return val or None
    return None


def save_message_id(msg_id: str) -> None:
    path = resolve_state_path("message_id.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(msg_id).strip() + "\n")


def load_totals_cache(server_id: str) -> dict:
    path = resolve_state_path("player_totals_cache.json")
    if not os.path.exists(path):
        return {"server_id": str(server_id or ""), "players": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and str(data.get("server_id", "")) == str(server_id or ""):
            if isinstance(data.get("players"), dict):
                return data
    except Exception:
        pass
    return {"server_id": str(server_id or ""), "players": {}}


def save_totals_cache(cache: dict) -> None:
    path = resolve_state_path("player_totals_cache.json")
    now = int(time.time())
    players = cache.get("players", {})
    # Prune stale entries older than 30 days so the cache stays compact
    pruned = {
        k: v
        for k, v in players.items()
        if isinstance(v, dict) and (now - int(v.get("fetched_at", 0))) <= CACHE_MAX_AGE_SECONDS
    }
    out = {"server_id": str(cache.get("server_id", "")), "players": pruned}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")


def load_api_cache(server_id: str) -> dict:
    path = resolve_state_path("wf_api_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and str(data.get("server_id", "")) == str(server_id or ""):
            return data
    except Exception:
        pass
    return {}


def save_api_cache(server_id: str, player_api_template: str = None) -> None:
    if not player_api_template:
        return
    # Safety check: never persist query tokens/keys if any appear in a URL
    low = player_api_template.lower()
    if any(tok in low for tok in ("apikey=", "api_key=", "token=", "secret=", "auth=")):
        return
    path = resolve_state_path("wf_api_cache.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "server_id": str(server_id or ""),
        "player_api_template": player_api_template,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def normalize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def extract_server_id(url: str):
    m = re.search(r"[?&]id=(\d+)", url or "")
    return m.group(1) if m else None


def make_progress_bar(current: int, maximum: int, length: int = 16) -> str:
    if not maximum or maximum <= 0:
        return ""
    pct = min(max(current / maximum, 0.0), 1.0)
    filled = round(pct * length)
    return f"`[{'■' * filled}{'□' * (length - filled)}]` {int(pct * 100)}%"


def strip_date_timestamps(text: str) -> str:
    """Remove Last Active date/clock timestamps like '11/19/2025, 1:46:27 PM' so only durations remain."""
    s = text or ""
    s = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?", " ", s, flags=re.I)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?", " ", s, flags=re.I)
    s = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)\b", " ", s, flags=re.I)
    return s


def parse_duration_seconds(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        if val < 0:
            return None
        return int(round(val))
    s = str(val).strip()
    if not s or s in ("—", "-", "--:--", "--:--:--", "N/A", "null", "None"):
        return None
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.match(r"^(\d+):(\d{2})$", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    total = 0.0
    matched = False
    for num, unit in re.findall(
        r"(\d+(?:\.\d+)?)\s*(days?|d|hrs?|hours?|h|mins?|minutes?|m|secs?|seconds?|s)\b",
        s,
        re.I,
    ):
        matched = True
        v = float(num)
        u = unit.lower()
        if u.startswith("d"):
            total += v * 86400
        elif u.startswith("h"):
            total += v * 3600
        elif u.startswith("m"):
            total += v * 60
        else:
            total += v
    if matched:
        return int(round(total))
    return None


def format_session_time(val) -> str:
    if isinstance(val, str):
        s = val.strip()
        if re.match(r"^\d+:\d{2}:\d{2}$", s):
            h, m, sec = s.split(":")
            return f"{int(h):02d}:{m}:{sec}"
    secs = parse_duration_seconds(val)
    if secs is None:
        return "--:--:--"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    res = f"{h:02d}:{m:02d}:{s:02d}"
    return res if len(res) <= 8 else f"{h}:{m:02d}"


def format_total_time(val) -> str:
    if isinstance(val, str):
        s = val.strip()
        m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s)
        if m:
            h, mn, sec = int(m.group(1)), m.group(2), m.group(3)
            out = f"{h:02d}:{mn}:{sec}"
            return out if len(out) <= 9 else f"{h}:{mn}"
        m2 = re.match(r"^(\d+):(\d{2})$", s)
        if m2:
            h, mn = int(m2.group(1)), m2.group(2)
            out = f"{h:02d}:{mn}:00"
            return out if len(out) <= 9 else f"{h}:{mn}"
    secs = parse_duration_seconds(val)
    if secs is None:
        return "--:--:--"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    out = f"{h:02d}:{m:02d}:{s:02d}"
    return out if len(out) <= 9 else f"{h}:{m:02d}"


def dedupe_player_entries(entries: list) -> list:
    seen = set()
    clean = []
    for item in entries:
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
        if not name or len(name) > 64:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        clean.append({
            "name": name,
            "href": item.get("href"),
            "session": item.get("session"),
            "total": item.get("total"),
            "session_secs": parse_duration_seconds(item.get("session")),
        })
    return clean


def convert_numeric_duration_by_key(key_lower: str, val):
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, (int, float)):
        if val < 0:
            return None
        if "ms" in key_lower or "milli" in key_lower:
            return int(round(val / 1000.0))
        if "hour" in key_lower or key_lower.endswith("hrs") or key_lower.endswith("_hr"):
            return int(round(val * 3600.0))
        if "min" in key_lower:
            return int(round(val * 60.0))
        return int(round(val))
    return None


def extract_players_from_list(items) -> list:
    entries = []
    if not isinstance(items, list):
        return entries
    for p in items:
        if isinstance(p, str):
            entries.append({"name": p, "href": None, "session": None, "total": None})
        elif isinstance(p, dict):
            pmap = {str(k).lower(): v for k, v in p.items()}
            name = None
            for key in ("name", "player_name", "playername", "username", "player", "driver", "nick"):
                val = pmap.get(key)
                if isinstance(val, str) and val.strip():
                    name = val.strip()
                    break
            if not name:
                continue

            session_val = None
            session_keys = (
                "session", "session_time", "sessiontime", "session_duration", "sessionduration",
                "session_seconds", "sessionseconds", "current_session", "currentsession",
                "connected", "connected_seconds", "online_time", "onlinetime", "duration", "time",
            )
            for key in session_keys:
                if key in pmap and pmap[key] is not None:
                    session_val = convert_numeric_duration_by_key(key, pmap[key])
                    break

            total_val = None
            server_total_keys = (
                "server_duration", "serverduration", "server_duration_seconds", "server_time",
                "servertime", "server_seconds", "serverseconds", "server_hours", "serverhours",
                "total_on_server", "total_server_duration", "total_duration", "totalduration",
                "total_seconds", "totalseconds", "total_hours", "totalhours", "total",
            )
            for key in server_total_keys:
                if key in pmap and pmap[key] is not None:
                    total_val = convert_numeric_duration_by_key(key, pmap[key])
                    break

            entries.append({"name": name, "href": None, "session": session_val, "total": total_val})
    return dedupe_player_entries(entries)


def extract_active_players_from_json(payloads) -> list:
    priority_keys = (
        "active_players", "activeplayers", "online_players", "onlineplayers",
        "current_players", "currentplayers", "live_players", "liveplayers",
        "player_list", "playerlist", "roster",
    )
    skip_parent_keys = ("leader", "top", "duration_leader", "history", "telemetry", "record", "all_time")

    def walk(obj, parent_key=""):
        if isinstance(obj, dict):
            lmap = {str(k).lower(): v for k, v in obj.items()}
            for k in priority_keys:
                if k in lmap and isinstance(lmap[k], list):
                    entries = extract_players_from_list(lmap[k])
                    if entries:
                        return entries
            if "players" in lmap and isinstance(lmap["players"], list):
                if not any(bad in parent_key for bad in skip_parent_keys):
                    entries = extract_players_from_list(lmap["players"])
                    if entries:
                        return entries
            for k, v in lmap.items():
                if any(bad in k for bad in skip_parent_keys):
                    continue
                res = walk(v, k)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = walk(item, parent_key)
                if res:
                    return res
        return []

    for payload in payloads:
        found = walk(payload)
        if found:
            return found
    return []


def extract_duration_leaders_map(full_text: str) -> dict:
    """Server-specific cumulative hours from the Duration Leaders Top 20 table on server.html."""
    leaders = {}
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    start = None
    for i, ln in enumerate(lines):
        if "duration leaders" in ln.lower():
            start = i
            break
    if start is None:
        return leaders

    end = min(len(lines), start + 160)
    for i in range(start + 1, end):
        if lines[i].lower() == "server details":
            end = i
            break

    section = lines[start + 1 : end]
    for i, ln in enumerate(section):
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)$", ln, re.I)
        if m and i > 0:
            prev = section[i - 1]
            if not re.match(r"^(\d+|rank|player|duration.*)$", prev, re.I):
                secs = int(round(float(m.group(1)) * 3600))
                leaders[prev.lower()] = secs
        else:
            m2 = re.match(r"^(\d+:\d{2}(?::\d{2})?)$", ln)
            if m2 and i > 0:
                prev = section[i - 1]
                if not re.match(r"^(\d+|rank|player|duration.*)$", prev, re.I):
                    leaders[prev.lower()] = m2.group(1)
    return leaders


async def extract_active_players_from_dom(page) -> tuple:
    js = r"""() => {
      const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,header'));
      let bestContainer = null;
      let bestLen = Infinity;

      for (const el of headings) {
        const own = (el.innerText || '').trim();
        if (!/^active\s+players\b/i.test(own) || own.length > 80) continue;

        let cur = el.parentElement;
        for (let depth = 0; depth < 6 && cur; depth++) {
          const txt = (cur.innerText || '').trim();
          const hasNextSection = /recent\s+events|duration\s+leaders|server\s+details/i.test(txt);
          if (!hasNextSection && txt.length >= own.length && txt.length < bestLen) {
            bestContainer = cur;
            bestLen = txt.length;
          }
          cur = cur.parentElement;
        }
      }

      if (!bestContainer) return { foundSection: false, empty: false, rows: [], sampleRaw: '' };

      const containerText = (bestContainer.innerText || '');
      if (/no\s+active\s+players/i.test(containerText)) {
        return { foundSection: true, empty: true, rows: [], sampleRaw: 'empty' };
      }

      const timeRe = /\b\d+:\d{2}(?::\d{2})?\b|\b\d+(?:\.\d+)?\s*(?:hrs?|hours?|h|mins?|minutes?|m|secs?|seconds?|s)\b/gi;
      const ignoredNameRe = /^(active\s+players.*|no\s+active\s+players.*|rank|player|player\s+name|driver|name|session|total|duration|score|ping|time|#|\d+)$/i;

      const parseRowElement = (rowEl) => {
        const rawText = (rowEl.innerText || '').trim();
        if (!rawText) return null;

        let name = '';
        let href = null;
        const link = rowEl.querySelector('a[href*="player"]');
        if (link && (link.innerText || '').trim()) {
          name = (link.innerText || '').trim();
          href = link.getAttribute('href') || null;
        }

        const cells = Array.from(rowEl.querySelectorAll(':scope > td, :scope > div, :scope > span'));
        const times = [];
        const cellTexts = cells.length >= 2 ? cells.map(c => (c.innerText || '').trim()) : [rawText];
        for (const ct of cellTexts) {
          const matches = ct.match(timeRe);
          if (matches) {
            for (const m of matches) times.push(m.trim());
          }
        }
        if (times.length === 0) {
          const fallbackMatches = rawText.match(timeRe) || [];
          for (const m of fallbackMatches) times.push(m.trim());
        }

        if (!name) {
          const parts = rawText.split(/\t|\r?\n/).map(s => s.trim()).filter(Boolean);
          for (const part of parts) {
            if (ignoredNameRe.test(part)) continue;
            if (/^\d+:\d{2}(?::\d{2})?$/.test(part)) continue;
            if (/^\d+(?:\.\d+)?\s*(?:hrs?|hours?|mins?|minutes?|secs?|seconds?)$/i.test(part)) continue;
            name = part;
            break;
          }
        }

        if (!name || ignoredNameRe.test(name)) return null;
        return {
          name,
          href,
          session: times.length >= 1 ? times[0] : null,
          total: times.length >= 2 ? times[1] : null,
          raw: rawText.replace(/\s+/g, ' ').slice(0, 140),
        };
      };

      let rowEls = Array.from(bestContainer.querySelectorAll('tbody tr'));
      if (rowEls.length === 0) {
        rowEls = Array.from(bestContainer.querySelectorAll('tr')).filter(r => !r.querySelector('th'));
      }
      if (rowEls.length === 0) {
        rowEls = Array.from(bestContainer.querySelectorAll('li'));
      }

      const parsedRows = [];
      for (const r of rowEls) {
        const item = parseRowElement(r);
        if (item) parsedRows.push(item);
      }
      if (parsedRows.length > 0) {
        return { foundSection: true, empty: false, rows: parsedRows, sampleRaw: parsedRows[0].raw };
      }

      const links = Array.from(bestContainer.querySelectorAll('a[href*="player"]'));
      for (const a of links) {
        let rowEl = a.closest('tr, li');
        if (!rowEl) {
          let cur = a.parentElement;
          while (cur && cur !== bestContainer) {
            const cLinks = cur.querySelectorAll('a[href*="player"]').length;
            if (cLinks === 1) {
              rowEl = cur;
            } else {
              break;
            }
            cur = cur.parentElement;
          }
        }
        const item = parseRowElement(rowEl || a);
        if (item) parsedRows.push(item);
      }
      if (parsedRows.length > 0) {
        return { foundSection: true, empty: false, rows: parsedRows, sampleRaw: parsedRows[0].raw };
      }

      const lines = containerText.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        if (ignoredNameRe.test(line)) continue;
        const times = line.match(timeRe) || [];
        let cleaned = line.replace(timeRe, '').replace(/^\s*\d+\s+/, '').trim();
        if (cleaned && !ignoredNameRe.test(cleaned)) {
          parsedRows.push({
            name: cleaned,
            href: null,
            session: times.length >= 1 ? times[0] : null,
            total: times.length >= 2 ? times[1] : null,
            raw: line.slice(0, 140),
          });
        }
      }
      return {
        foundSection: true,
        empty: false,
        rows: parsedRows,
        sampleRaw: parsedRows.length ? parsedRows[0].raw : '',
      };
    }"""
    try:
        res = await page.evaluate(js)
        if res and res.get("foundSection"):
            if res.get("empty"):
                return [], "empty"
            return dedupe_player_entries(res.get("rows", [])), res.get("sampleRaw", "")
    except Exception:
        pass
    return [], ""


def looks_like_player_profile_json(obj) -> bool:
    if isinstance(obj, dict):
        lkeys = {str(k).lower() for k in obj.keys()}
        server_list_keys = {"servers", "server_history", "serverhistory", "server_stats", "serverstats", "history", "visits"}
        if lkeys & server_list_keys:
            return True
        if "total_duration" in lkeys or "totalduration" in lkeys or "servers_visited" in lkeys:
            return True
        for v in obj.values():
            if isinstance(v, (dict, list)) and looks_like_player_profile_json(v):
                return True
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        lkeys = {str(k).lower() for k in obj[0].keys()}
        if any(k in lkeys for k in ("server_id", "serverid", "server_name", "servername")):
            return True
    return False


def pick_duration_from_server_row_dict(lmap: dict):
    skip_tokens = ("last", "first", "seen", "active", "updated", "created", "date", "timestamp")
    preferred_keys = (
        "duration", "duration_seconds", "durationseconds", "total_duration", "totalduration",
        "total_duration_seconds", "total_seconds", "totalseconds", "seconds", "total_time",
        "totaltime", "playtime", "time_played", "timeplayed", "hours", "total_hours",
        "duration_hours", "minutes", "duration_ms", "time",
    )
    for k in preferred_keys:
        if k in lmap and lmap[k] is not None:
            cand = convert_numeric_duration_by_key(k, lmap[k])
            if parse_duration_seconds(cand) is not None:
                return cand
    for k, v in lmap.items():
        if v is None or isinstance(v, (dict, list, bool)):
            continue
        if any(bad in k for bad in skip_tokens):
            continue
        if any(tok in k for tok in ("dur", "sec", "hour", "hr", "min", "play", "time")):
            cand = convert_numeric_duration_by_key(k, v)
            if parse_duration_seconds(cand) is not None:
                return cand
    return None


def extract_server_total_from_player_json(payloads, server_id: str, server_name: str, base_name: str):
    """Extract ONLY the player's duration on the target Get ReKt420 server from player profile JSON."""
    exact_norm = normalize_name(server_name)
    base_norm = normalize_name(base_name)
    tier1_hits = []
    tier2_hits = []
    tier3_hits = []
    saw_server_list = False

    def inspect_row(d: dict):
        nonlocal saw_server_list
        lmap = {str(k).lower(): v for k, v in d.items()}

        row_sid = None
        for k in ("server_id", "serverid", "sid"):
            if k in lmap and lmap[k] is not None:
                row_sid = str(lmap[k]).strip()
                break

        row_sname = None
        for k in ("server_name", "servername", "server"):
            v = lmap.get(k)
            if isinstance(v, str) and v.strip():
                row_sname = v.strip()
                break

        if row_sname is None and isinstance(lmap.get("name"), str):
            if row_sid is not None or any(
                k in lmap for k in ("last_active", "lastactive", "last_seen", "lastseen", "duration", "duration_seconds", "hours")
            ):
                row_sname = lmap["name"].strip()
        if row_sid is None and lmap.get("id") is not None and row_sname is not None:
            row_sid = str(lmap["id"]).strip()

        if row_sid is None and row_sname is None:
            return

        dur = pick_duration_from_server_row_dict(lmap)
        if dur is None:
            return

        saw_server_list = True
        norm_row = normalize_name(row_sname) if row_sname else ""

        if server_id and row_sid == str(server_id):
            tier1_hits.append(dur)
        elif exact_norm and norm_row == exact_norm:
            tier2_hits.append(dur)
        elif base_norm and base_norm in norm_row:
            tier3_hits.append(dur)

    def walk(obj):
        if isinstance(obj, dict):
            inspect_row(obj)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item)

    for payload in payloads:
        walk(payload)

    if tier1_hits:
        return True, tier1_hits[0]
    if tier2_hits:
        return True, tier2_hits[0]
    if tier3_hits:
        return True, tier3_hits[0]
    return saw_server_list, None


async def extract_server_total_from_player_dom(page, server_id: str, server_name: str, base_name: str):
    js = r"""({ serverId, exactNorm, baseNorm }) => {
      const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
      const stripDates = s => (s || '')
        .replace(/\b\d{1,2}\/\d{1,2}\/\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?/gi, ' ')
        .replace(/\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?/gi, ' ')
        .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)\b/gi, ' ');

      const extractDur = (txt) => {
        const cleaned = stripDates(txt);
        const m = cleaned.match(/\b(\d+:\d{2}:\d{2})\b/);
        if (m) return m[1];
        const m2 = cleaned.match(/\b(\d+(?:\.\d+)?\s*(?:hrs?|hours?))\b/i);
        if (m2) return m2[1];
        return null;
      };

      const isBadContainer = (el) => {
        if (!el) return true;
        if (el.closest('header, nav, footer')) return true;
        const t = (el.innerText || '').toLowerCase();
        if (t.includes('cultivated by') || t.includes('discord.gg') || t.includes('driver profile telemetry')) {
          return true;
        }
        return false;
      };

      const links = Array.from(document.querySelectorAll('a[href*="server.html"]')).filter(a => !isBadContainer(a));
      let hasAnyServerRows = links.length > 0;

      const findRowForEl = (el) => {
        let row = el.closest('tr, li');
        if (row && !isBadContainer(row)) return row;
        let cur = el.parentElement;
        while (cur && !isBadContainer(cur)) {
          if (cur.querySelectorAll('a[href*="server.html"]').length === 1) {
            row = cur;
            cur = cur.parentElement;
          } else {
            break;
          }
        }
        return row || el;
      };

      if (serverId) {
        const idRe = new RegExp('[?&]id=' + serverId + '(?:&|$)');
        for (const a of links) {
          const href = a.getAttribute('href') || '';
          if (idRe.test(href)) {
            const row = findRowForEl(a);
            const dur = extractDur(row.innerText || '');
            if (dur) return { loaded: true, duration: dur };
          }
        }
      }

      let baseHit = null;
      for (const a of links) {
        const txtNorm = norm(a.innerText || '');
        if (!txtNorm) continue;
        const row = findRowForEl(a);
        const dur = extractDur(row.innerText || '');
        if (!dur) continue;
        if (exactNorm && txtNorm === exactNorm) {
          return { loaded: true, duration: dur };
        }
        if (baseNorm && txtNorm.includes(baseNorm) && !baseHit) {
          baseHit = dur;
        }
      }
      if (baseHit) return { loaded: true, duration: baseHit };

      const rows = Array.from(document.querySelectorAll('tbody tr, table tr')).filter(r => !r.querySelector('th') && !isBadContainer(r));
      if (rows.length > 0) hasAnyServerRows = true;
      for (const r of rows) {
        const t = r.innerText || '';
        const tn = norm(t);
        const dur = extractDur(t);
        if (!dur) continue;
        if (exactNorm && tn.includes(exactNorm)) {
          return { loaded: true, duration: dur };
        }
        if (baseNorm && tn.includes(baseNorm) && !baseHit) {
          baseHit = dur;
        }
      }
      if (baseHit) return { loaded: true, duration: baseHit };

      return { loaded: hasAnyServerRows, duration: null };
    }"""
    try:
        res = await page.evaluate(
            js,
            {
                "serverId": str(server_id) if server_id else "",
                "exactNorm": normalize_name(server_name),
                "baseNorm": normalize_name(base_name),
            },
        )
        if res:
            return bool(res.get("loaded")), res.get("duration")
    except Exception:
        pass
    return False, None


def extract_server_total_from_player_text(text: str, server_name: str, base_name: str):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    exact_norm = normalize_name(server_name)
    base_norm = normalize_name(base_name)

    start_idx = 0
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "last active" in low or "servers visited" in low:
            start_idx = i + 1

    def find_duration_near(idx):
        for offset in range(0, 3):
            if idx + offset >= len(lines):
                break
            cand_line = lines[idx + offset]
            if offset > 0 and ("cultivated by" in cand_line.lower() or "discord.gg" in cand_line.lower()):
                break
            cleaned = strip_date_timestamps(cand_line)
            m = re.search(r"\b(\d+:\d{2}:\d{2})\b", cleaned)
            if m:
                return m.group(1)
            m2 = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\b", cleaned, re.I)
            if m2:
                return int(round(float(m2.group(1)) * 3600))
        return None

    base_fallback = None
    for i in range(start_idx, len(lines)):
        low = lines[i].lower()
        if "cultivated by" in low or "discord.gg" in low or "total duration" in low:
            continue
        ln_norm = normalize_name(lines[i])
        if exact_norm and exact_norm in ln_norm:
            dur = find_duration_near(i)
            if dur is not None:
                return dur
        elif base_norm and base_norm in ln_norm and base_fallback is None:
            dur = find_duration_near(i)
            if dur is not None:
                base_fallback = dur

    return base_fallback


def apply_cached_totals(players: list, cache: dict, now_ts: int) -> tuple:
    """
    Fill fresh cached Get ReKt420 totals immediately (and advance them with live session delta).
    Returns (players_needing_fetch, stale_fallbacks_by_lower_name).
    """
    cached_map = cache.get("players", {})
    to_fetch = []
    stale_fallbacks = {}

    for p in players:
        if parse_duration_seconds(p.get("total")) is not None:
            continue
        low = p["name"].lower()
        entry = cached_map.get(low)
        cur_sess = p.get("session_secs")

        if isinstance(entry, dict) and entry.get("total_secs") is not None:
            base_total = int(entry["total_secs"])
            fetched_at = int(entry.get("fetched_at", 0))
            prev_sess = entry.get("session_secs_at_fetch")

            if cur_sess is not None and prev_sess is not None and cur_sess >= int(prev_sess):
                estimated = base_total + (cur_sess - int(prev_sess))
            else:
                estimated = base_total

            if (now_ts - fetched_at) < CACHE_TTL_SECONDS:
                p["total"] = estimated
            else:
                stale_fallbacks[low] = estimated
                to_fetch.append((1, fetched_at, p))  # priority 1 = stale refresh
        else:
            to_fetch.append((0, 0, p))  # priority 0 = brand-new player (highest priority)

    # Fetch brand-new players first, then oldest stale entries
    to_fetch.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in to_fetch], stale_fallbacks


def fetch_via_http_api_batch(api_template: str, players: list, server_id: str, server_name: str, base_name: str) -> dict:
    """Concurrently fetch server-only totals via discovered JSON API using 8 threads."""
    results = {}

    def fetch_one(p):
        try:
            api_url = api_template.replace("{NAME}", quote(p["name"], safe=""))
            resp = requests.get(api_url, timeout=6)
            if resp.status_code == 200:
                loaded, tot = extract_server_total_from_player_json(
                    [resp.json()], server_id, server_name, base_name
                )
                if tot is not None:
                    return p["name"].lower(), parse_duration_seconds(tot)
                if loaded:
                    return p["name"].lower(), 0
        except Exception:
            pass
        return p["name"].lower(), None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for low, secs in pool.map(fetch_one, players):
            if secs is not None:
                results[low] = secs
    return results


async def block_heavy_assets(route):
    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()


async def enrich_missing_totals_fast(
    context,
    players: list,
    cache: dict,
    api_cache: dict,
    server_id: str,
    server_name: str,
    base_name: str,
) -> bool:
    """
    Fast Get ReKt420 server-only Total Duration loader:
      1. Uses fresh player_totals_cache.json entries immediately (0ms).
      2. Tries cached JSON API template in parallel (8 threads, ~1s for all 24 players).
      3. Falls back to 5 concurrent lightweight Playwright tabs with a hard 45s time budget.
    Returns True if cache was updated.
    """
    now_ts = int(time.time())
    to_fetch, stale_fallbacks = apply_cached_totals(players, cache, now_ts)
    if not to_fetch:
        print(f"All {len(players)} player totals served instantly from cache!")
        return False

    t_start = time.monotonic()
    cache_dirty = False
    cached_map = cache.setdefault("players", {})

    def record_total(p, secs: int):
        nonlocal cache_dirty
        p["total"] = secs
        cached_map[p["name"].lower()] = {
            "total_secs": int(secs),
            "session_secs_at_fetch": p.get("session_secs"),
            "fetched_at": int(time.time()),
        }
        cache_dirty = True

    def build_profile_url(p):
        if p.get("href"):
            return urljoin(TARGET_URL, p["href"])
        return f"https://www.wfservers.com/player.html?name={quote(p['name'], safe='')}"

    # Fast Path A: Try already-known JSON API template first (no Playwright tabs needed!)
    discovered_api = api_cache.get("player_api_template")
    if discovered_api:
        hits = fetch_via_http_api_batch(discovered_api, to_fetch, server_id, server_name, base_name)
        for p in to_fetch:
            secs = hits.get(p["name"].lower())
            if secs is not None:
                record_total(p, secs)
        to_fetch = [p for p in to_fetch if parse_duration_seconds(p.get("total")) is None]
        if not to_fetch:
            print(f"Fetched {len(hits)} player totals via fast JSON API in {time.monotonic() - t_start:.1f}s!")
            return cache_dirty

    # Fast Path B: Probe 1 player via Playwright to discover the JSON API template
    if to_fetch and not discovered_api and (time.monotonic() - t_start) < ENRICH_TIME_BUDGET_SECONDS:
        first = to_fetch[0]
        probe_page = await context.new_page()
        await probe_page.route("**/*", block_heavy_assets)
        first_json = []
        first_enc = quote(first["name"], safe="")
        first_low = first["name"].lower()

        async def handle_probe_resp(response):
            nonlocal discovered_api
            try:
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                data = await response.json()
                if looks_like_player_profile_json(data):
                    first_json.append(data)
                    u = response.url
                    if first_enc in u:
                        discovered_api = u.replace(first_enc, "{NAME}")
                    elif first_low in unquote(u).lower():
                        raw_dec = unquote(u)
                        idx = raw_dec.lower().find(first_low)
                        orig_slice = raw_dec[idx : idx + len(first["name"])]
                        if quote(orig_slice, safe="") in u:
                            discovered_api = u.replace(quote(orig_slice, safe=""), "{NAME}")
                        elif orig_slice in u:
                            discovered_api = u.replace(orig_slice, "{NAME}")
            except Exception:
                pass

        probe_page.on("response", lambda r: asyncio.create_task(handle_probe_resp(r)))
        try:
            await probe_page.goto(build_profile_url(first), wait_until="domcontentloaded", timeout=15000)
            try:
                await probe_page.wait_for_function(
                    "() => document.querySelectorAll('a[href*=\"server.html\"]').length > 0 || /servers\\s+visited/i.test(document.body.innerText)",
                    timeout=6000,
                )
            except Exception:
                pass
            await probe_page.wait_for_timeout(400)

            loaded_j, tot = extract_server_total_from_player_json(first_json, server_id, server_name, base_name)
            loaded_d = False
            if tot is None:
                loaded_d, tot = await extract_server_total_from_player_dom(probe_page, server_id, server_name, base_name)
            if tot is None:
                tot = extract_server_total_from_player_text(await probe_page.locator("body").inner_text(), server_name, base_name)

            secs = parse_duration_seconds(tot)
            if secs is not None:
                record_total(first, secs)
            elif loaded_j or loaded_d:
                record_total(first, 0)
        except Exception as e:
            print(f"Probe error for {first['name']}: {e}")
        finally:
            await probe_page.close()

        to_fetch = [p for p in to_fetch if parse_duration_seconds(p.get("total")) is None]

        # If probe discovered a working JSON API, burst-fetch all remaining players via HTTP threads!
        if discovered_api and to_fetch:
            save_api_cache(server_id, discovered_api)
            hits = fetch_via_http_api_batch(discovered_api, to_fetch, server_id, server_name, base_name)
            for p in to_fetch:
                secs = hits.get(p["name"].lower())
                if secs is not None:
                    record_total(p, secs)
            to_fetch = [p for p in to_fetch if parse_duration_seconds(p.get("total")) is None]

    # Fast Path C: Fetch any remaining players in parallel using 5 lightweight Playwright tabs + hard time budget
    if to_fetch and (time.monotonic() - t_start) < ENRICH_TIME_BUDGET_SECONDS:
        print(f"Fetching {len(to_fetch)} player profiles concurrently ({PROFILE_CONCURRENCY} tabs)...")
        sem = asyncio.Semaphore(PROFILE_CONCURRENCY)

        async def fetch_tab(p):
            if (time.monotonic() - t_start) >= ENRICH_TIME_BUDGET_SECONDS:
                return
            async with sem:
                if (time.monotonic() - t_start) >= ENRICH_TIME_BUDGET_SECONDS:
                    return
                page = await context.new_page()
                await page.route("**/*", block_heavy_assets)
                p_json = []

                async def cap(resp):
                    try:
                        if "json" in resp.headers.get("content-type", "").lower():
                            data = await resp.json()
                            if looks_like_player_profile_json(data):
                                p_json.append(data)
                    except Exception:
                        pass

                page.on("response", lambda r: asyncio.create_task(cap(r)))
                try:
                    await page.goto(build_profile_url(p), wait_until="domcontentloaded", timeout=12000)
                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('a[href*=\"server.html\"]').length > 0 || /last\\s+active/i.test(document.body.innerText)",
                            timeout=5000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(250)
                    loaded_j, tot = extract_server_total_from_player_json(p_json, server_id, server_name, base_name)
                    loaded_d = False
                    if tot is None:
                        loaded_d, tot = await extract_server_total_from_player_dom(page, server_id, server_name, base_name)
                    if tot is None:
                        tot = extract_server_total_from_player_text(await page.locator("body").inner_text(), server_name, base_name)
                    secs = parse_duration_seconds(tot)
                    if secs is not None:
                        record_total(p, secs)
                    elif loaded_j or loaded_d:
                        record_total(p, 0)
                except Exception:
                    pass
                finally:
                    await page.close()

        await asyncio.gather(*(fetch_tab(p) for p in to_fetch))

    # If time budget cut off a stale refresh, preserve their previous cached value instead of showing --:--:--
    for p in players:
        if parse_duration_seconds(p.get("total")) is None:
            fb = stale_fallbacks.get(p["name"].lower())
            if fb is not None:
                p["total"] = fb

    print(f"Profile enrichment finished in {time.monotonic() - t_start:.1f}s.")
    return cache_dirty


def extract_detail_fields_from_text(full_text: str) -> dict:
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    info = {
        "name": None,
        "online": None,
        "current": None,
        "maximum": None,
    }

    for ln in lines[:25]:
        if ln.upper() == "ONLINE":
            info["online"] = True
            break
        if ln.upper() == "OFFLINE":
            info["online"] = False
            break

    for i, ln in enumerate(lines):
        if "players / capacity" in ln.lower():
            for nxt in lines[i : i + 4]:
                m = re.search(r"(\d+)\s*/\s*(\d+)", nxt)
                if m:
                    info["current"], info["maximum"] = int(m.group(1)), int(m.group(2))
                    break
            break

    detail_start = None
    for i, ln in enumerate(lines):
        if ln.lower() == "server details":
            detail_start = i
            break

    if detail_start is not None:
        detail_lines = lines[detail_start + 1 : detail_start + 70]
        label_map = {
            "name": "name",
            "players": "players_val",
            "max players": "max_val",
        }
        for i, ln in enumerate(detail_lines):
            key = ln.lstrip("*•- ").rstrip(":").strip().lower()
            if key in label_map and i + 1 < len(detail_lines):
                val = detail_lines[i + 1].strip()
                if val in ("—", "-", ""):
                    continue
                target = label_map[key]
                if target == "players_val" and info["current"] is None:
                    m = re.search(r"\d+", val)
                    if m:
                        info["current"] = int(m.group(0))
                elif target == "max_val" and info["maximum"] is None:
                    m = re.search(r"\d+", val)
                    if m:
                        info["maximum"] = int(m.group(0))
                elif target == "name" and info["name"] is None:
                    info["name"] = val

    return info


async def discover_server_url_fallback(page, target_norm: str) -> str:
    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_function("() => document.querySelectorAll('a[href*=\"server.html\"]').length > 0", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1200)

    js = r"""(targetNorm) => {
      const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
      const candidates = [];
      for (const a of document.querySelectorAll('a[href*="server.html"]')) {
        const href = a.getAttribute('href') || '';
        const row = a.closest('tr, [class*="server"], [class*="row"], [class*="card"], li') || a;
        const txt = (row.innerText || a.innerText || '').trim();
        if (norm(txt).includes(targetNorm)) {
          const m = txt.match(/(\d+)\s*\/\s*(\d+)/);
          const players = m ? parseInt(m[1], 10) : -1;
          candidates.push({ href, players, len: txt.length });
        }
      }
      candidates.sort((a, b) => (b.players - a.players) || (a.len - b.len));
      return candidates.length ? candidates[0].href : null;
    }"""
    try:
        href = await page.evaluate(js, target_norm)
        if href:
            return urljoin(TARGET_URL, href)
    except Exception:
        pass
    return SERVER_PAGE_URL


async def fetch_server_status_async() -> dict:
    t0 = time.monotonic()
    target_norm = normalize_name(SERVER_NAME)
    captured_json = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.route("**/*", block_heavy_assets)

        server_url = SERVER_PAGE_URL.strip()
        if not server_url:
            server_url = await discover_server_url_fallback(page, target_norm)
        server_id = extract_server_id(server_url)

        async def handle_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype.lower():
                    captured_json.append(await response.json())
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        # Navigate directly to server.html?id=... with 1 automatic retry
        loaded_ok = False
        for attempt in range(2):
            try:
                await page.goto(server_url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_function(
                    "() => !document.body.innerText.includes('LOADING SERVER TELEMETRY') && (/active\\s+players/i.test(document.body.innerText) || /server\\s+details/i.test(document.body.innerText))",
                    timeout=15000,
                )
                loaded_ok = True
                break
            except Exception as e:
                print(f"Server page attempt {attempt + 1} slow/failed: {e}")
                if attempt == 0:
                    await page.wait_for_timeout(1500)

        if loaded_ok:
            await page.wait_for_timeout(1200)

        dom_players, sample_raw = await extract_active_players_from_dom(page)
        full_text = await page.locator("body").inner_text()
        parsed = extract_detail_fields_from_text(full_text)

        # If header shows >0 players but active player rows are still hydrating, wait briefly and re-read
        if (parsed["current"] or 0) > 0 and not dom_players:
            await page.wait_for_timeout(3000)
            dom_players, sample_raw = await extract_active_players_from_dom(page)
            full_text = await page.locator("body").inner_text()
            parsed = extract_detail_fields_from_text(full_text)

        json_players = extract_active_players_from_json(captured_json)
        leaders_map = extract_duration_leaders_map(full_text)

        if dom_players:
            json_by_name = {pl["name"].lower(): pl for pl in json_players}
            players = []
            for pl in dom_players:
                low = pl["name"].lower()
                jp = json_by_name.get(low, {})
                session = pl["session"] if pl["session"] is not None else jp.get("session")
                total = pl["total"] if pl["total"] is not None else jp.get("total")
                players.append({
                    "name": pl["name"],
                    "href": pl.get("href"),
                    "session": session,
                    "total": total,
                    "session_secs": parse_duration_seconds(session),
                })
        else:
            players = []
            for pl in json_players:
                players.append({
                    "name": pl["name"],
                    "href": pl.get("href"),
                    "session": pl["session"],
                    "total": pl["total"],
                    "session_secs": parse_duration_seconds(pl["session"]),
                })

        if any(pl["session_secs"] is not None for pl in players):
            players.sort(
                key=lambda pl: (pl["session_secs"] if pl["session_secs"] is not None else -1),
                reverse=True,
            )

        players = players[:MAX_PLAYERS_SHOWN]
        display_server_name = parsed["name"] or SERVER_NAME

        totals_cache = load_totals_cache(server_id)
        api_cache = load_api_cache(server_id)

        if players:
            cache_dirty = await enrich_missing_totals_fast(
                context, players, totals_cache, api_cache, server_id, display_server_name, SERVER_NAME
            )
            if cache_dirty:
                save_totals_cache(totals_cache)

        # Fallback: Duration Leaders Top 20 on this exact server page
        for pl in players:
            if parse_duration_seconds(pl.get("total")) is None:
                fb = leaders_map.get(pl["name"].lower())
                if fb is not None:
                    pl["total"] = fb

        current = parsed["current"] if parsed["current"] is not None else len(players)
        maximum = parsed["maximum"] if parsed["maximum"] is not None else 24

        if current == 0:
            players = []

        roster_png_path = os.path.join(BASE_DIR, ROSTER_IMAGE_FILENAME)
        has_roster_png = await render_roster_png_async(browser, players, roster_png_path)
        await browser.close()

    found = parsed["online"] is not False and (parsed["online"] is True or parsed["maximum"] is not None)
    print(f"Total scrape time: {time.monotonic() - t0:.1f}s ({current}/{maximum} players, png={has_roster_png})")

    return {
        "found": found,
        "server_name": display_server_name,
        "server_url": server_url,
        "current": current,
        "maximum": maximum,
        "players": players,
        "roster_png_path": roster_png_path if has_roster_png else None,
    }


def build_roster_html(players: list) -> str:
    rows_html = []
    if not players:
        rows_html.append(
            '<div class="empty-row">No active players online right now</div>'
        )
    else:
        for rank, p in enumerate(players[:MAX_PLAYERS_SHOWN], start=1):
            if rank == 1:
                rank_cls = "rank-1"
            elif rank == 2:
                rank_cls = "rank-2"
            elif rank == 3:
                rank_cls = "rank-3"
            else:
                rank_cls = "rank-std"

            raw_name = str(p.get("name") or "").strip()
            safe_name = html.escape(raw_name)
            sess = html.escape(format_session_time(p.get("session")))
            tot = html.escape(format_total_time(p.get("total")))

            rows_html.append(
                f'<div class="row">'
                f'<div class="col-rank"><span class="badge {rank_cls}">[{rank}]</span></div>'
                f'<div class="col-name">{safe_name}</div>'
                f'<div class="col-sess">{sess}</div>'
                f'<div class="col-tot">{tot}</div>'
                f'</div>'
            )

    body_rows = "\n".join(rows_html)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #2b2d31;
    font-family: 'Liberation Sans', 'DejaVu Sans', Arial, sans-serif;
    color: #ffffff;
    width: 420px;
  }}
  #roster-card {{
    width: 420px;
    background: #1e1f22;
    border: 1px solid #383a40;
    border-radius: 8px;
    overflow: hidden;
  }}
  .header {{
    display: flex;
    align-items: center;
    padding: 8px 8px;
    background: #17181b;
    border-bottom: 1px solid #32343a;
    font-family: 'Liberation Sans', 'DejaVu Sans', Arial, sans-serif;
    font-size: 13.5px;
    font-weight: 700;
    letter-spacing: 0.3px;
    color: #dcdfe4;
    text-transform: uppercase;
  }}
  .row {{
    display: flex;
    align-items: center;
    padding: 5px 8px;
    border-bottom: 1px solid #26282d;
    font-size: 18px;
    line-height: 1.2;
  }}
  .row:nth-child(even) {{
    background: #222428;
  }}
  .row:last-child {{
    border-bottom: none;
  }}
  .col-rank {{
    width: 48px;
    flex-shrink: 0;
    font-family: 'Liberation Mono', 'DejaVu Sans Mono', monospace;
    font-size: 15px;
    font-weight: 700;
  }}
  .badge {{
    display: inline-block;
    min-width: 42px;
    text-align: center;
    padding: 2px 3px;
    border-radius: 4px;
    letter-spacing: -0.5px;
  }}
  .rank-1 {{
    color: #f5c542;
    background: rgba(245, 197, 66, 0.14);
    border: 1px solid rgba(245, 197, 66, 0.45);
  }}
  .rank-2 {{
    color: #c5cbd3;
    background: rgba(197, 203, 211, 0.14);
    border: 1px solid rgba(197, 203, 211, 0.40);
  }}
  .rank-3 {{
    color: #e8833a;
    background: rgba(232, 131, 58, 0.14);
    border: 1px solid rgba(232, 131, 58, 0.45);
  }}
  .rank-std {{
    color: #ffffff;
    background: #2b2d31;
    border: 1px solid #3a3d44;
  }}
  .col-name {{
    flex: 1;
    min-width: 0;
    padding-right: 6px;
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .col-sess {{
    width: 86px;
    flex-shrink: 0;
    text-align: right;
    font-family: 'Liberation Mono', 'DejaVu Sans Mono', monospace;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #ffffff;
    font-variant-numeric: tabular-nums;
  }}
  .col-tot {{
    width: 96px;
    flex-shrink: 0;
    text-align: right;
    font-family: 'Liberation Mono', 'DejaVu Sans Mono', monospace;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: #ffffff;
    font-variant-numeric: tabular-nums;
  }}
  .header .col-rank,
  .header .col-name,
  .header .col-sess,
  .header .col-tot {{
    font-family: 'Liberation Sans', 'DejaVu Sans', Arial, sans-serif;
    font-size: 13.5px;
    font-weight: 700;
    letter-spacing: 0.3px;
    color: #dcdfe4;
  }}
  .empty-row {{
    padding: 20px 10px;
    text-align: center;
    color: #dcdfe4;
    font-size: 17px;
    font-weight: 600;
  }}
</style>
</head>
<body>
  <div id="roster-card">
    <div class="header">
      <div class="col-rank">Rank</div>
      <div class="col-name">Player</div>
      <div class="col-sess">Session</div>
      <div class="col-tot">Total</div>
    </div>
    {body_rows}
  </div>
</body>
</html>"""


async def render_roster_png_async(browser, players: list, out_path: str) -> bool:
    """Render a crisp 2x retina PNG table so Desktop and Mobile Discord look identical."""
    try:
        ctx = await browser.new_context(
            viewport={"width": 460, "height": 1400},
            device_scale_factor=2,
        )
        page = await ctx.new_page()
        await page.set_content(build_roster_html(players), wait_until="domcontentloaded")
        card = page.locator("#roster-card")
        await card.screenshot(path=out_path, type="png")
        await ctx.close()
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        print(f"Roster PNG render fallback triggered: {e}")
        return False


def build_mobile_safe_text_fallback(players: list) -> str:
    """32-char plain monospace fallback (no ANSI codes) if PNG rendering ever fails."""
    lines = [f"{'#':<3} {'Player':<13} {'Sess':>6} {'Total':>7}", "-" * 32]
    if not players:
        lines.append("  No active players online")
    else:
        for rank, p in enumerate(players[:MAX_PLAYERS_SHOWN], start=1):
            r_str = f"{rank}."
            nm = p["name"].replace("`", "'")
            nm = (nm[:12] + "…") if len(nm) > 13 else nm
            s_full = format_session_time(p.get("session"))
            s_short = s_full[:5] if s_full != "--:--:--" else "--:--"
            t_full = format_total_time(p.get("total"))
            t_short = t_full[:-3] if len(t_full) >= 8 and t_full != "--:--:--" else "--:--"
            lines.append(f"{r_str:<3} {nm:<13} {s_short:>6} {t_short:>7}")
    return "```\n" + "\n".join(lines) + "\n```"


def build_embed(info: dict, thumbnail_url: str = THUMBNAIL_URL, use_image: bool = True, is_patch: bool = False) -> dict:
    found = info["found"]
    current = info["current"]
    maximum = info["maximum"]
    display_name = info.get("server_name") or SERVER_NAME
    card_url = info.get("server_url") or TARGET_URL
    players = info.get("players", [])

    if not found:
        color = 0x95A5A6
        status_line = "⚪ Offline"
        bar = ""
    elif current > 0:
        color = 0x2ECC71
        status_line = f"🟢 Online — {current} / {maximum} Players"
        bar = make_progress_bar(current, maximum)
    else:
        color = 0xE67E22
        status_line = f"🟠 Online (Empty) — 0 / {maximum} Players"
        bar = make_progress_bar(0, maximum)

    roster_heading = f"### Active Players ({current}/{maximum})" if maximum else f"### Active Players ({current})"

    desc_parts = [f"**{status_line}**"]
    if bar:
        desc_parts.append(bar)
    desc_parts.append("")
    desc_parts.append(roster_heading)
    if not use_image:
        desc_parts.append(build_mobile_safe_text_fallback(players))
    description = "\n".join(desc_parts)

    embed = {
        "author": {
            "name": "wfservers.com — Server Tracker",
            "url": TARGET_URL,
        },
        "title": f"Wreckfest — {display_name}",
        "url": card_url,
        "description": description,
        "color": color,
        "footer": {"text": "Source: wfservers.com • 24/7 Cloud Auto-Update"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}

    payload = {"embeds": [embed]}
    if use_image:
        embed["image"] = {"url": f"attachment://{ROSTER_IMAGE_FILENAME}"}
        # On PATCH, attachments=[] clears the old PNG and replaces it with files[0] cleanly
        payload["attachments"] = [] if is_patch else [{"id": 0, "filename": ROSTER_IMAGE_FILENAME}]
    elif is_patch:
        payload["attachments"] = []

    return payload


def send_discord_request(method: str, url: str, payload: dict, image_path: str = None):
    img_bytes = None
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img_bytes = f.read()

    for attempt in range(3):
        if img_bytes is not None:
            res = requests.request(
                method,
                url,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (ROSTER_IMAGE_FILENAME, img_bytes, "image/png")},
                timeout=25,
            )
        else:
            res = requests.request(method, url, json=payload, timeout=20)

        if res.status_code == 429:
            retry_after = float(res.json().get("retry_after", 2))
            time.sleep(min(retry_after, 10))
            continue
        if res.status_code >= 500 and attempt < 2:
            time.sleep(2)
            continue
        return res
    return res


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not is_valid_webhook(webhook_url):
        print("ERROR: Missing or invalid DISCORD_WEBHOOK_URL secret.")
        sys.exit(1)

    info = asyncio.run(fetch_server_status_async())
    png_path = info.get("roster_png_path")
    use_img = bool(png_path and os.path.exists(png_path))
    message_id = load_message_id()

    try:
        if message_id:
            payload = build_embed(info, use_image=use_img, is_patch=True)
            res = send_discord_request("PATCH", f"{webhook_url}/messages/{message_id}", payload, png_path if use_img else None)
            if res.status_code == 404:
                print("Previous Discord card was deleted. Posting a new one...")
                message_id = None
            else:
                res.raise_for_status()
                with_totals = sum(1 for pl in info["players"] if parse_duration_seconds(pl.get("total")) is not None)
                print(
                    f"Updated card: {info['current']}/{info['maximum']} players "
                    f"({len(info['players'])} listed, {with_totals} with Get ReKt420 Total, mobile_png={use_img})"
                )

        if not message_id:
            payload = build_embed(info, use_image=use_img, is_patch=False)
            res = send_discord_request("POST", f"{webhook_url}?wait=true", payload, png_path if use_img else None)
            res.raise_for_status()
            new_id = res.json()["id"]
            save_message_id(new_id)
            print(f"Posted new live Discord card! Message ID saved: {new_id}")
    finally:
        # Remove temporary PNG so git commit step never bloats the repository
        if png_path and os.path.exists(png_path):
            try:
                os.remove(png_path)
            except Exception:
                pass


if __name__ == "__main__":
    main()
