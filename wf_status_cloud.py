import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin
import requests
from playwright.sync_api import sync_playwright

SERVER_NAME = "Get ReKt420"
TARGET_URL = "https://wfservers.com"
# Optional: paste a direct wfservers.com/server.html?id=... link here to lock onto one exact server.
# Leave blank to auto-find Get ReKt420 on wfservers.com (falls back to Midwest server #309494).
SERVER_PAGE_URL = ""
FALLBACK_SERVER_URL = "https://www.wfservers.com/server.html?id=309494"
MAX_PLAYERS_SHOWN = 24

THUMBNAIL_CANDIDATES = [
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/228380/header.jpg",
    "https://steamcdn-a.akamaihd.net/steam/apps/228380/header.jpg",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_msg_id_path() -> str:
    candidates = [
        os.path.join(BASE_DIR, "message_id.txt"),
        os.path.join(BASE_DIR, "UPLOAD_INSIDE_TO_GITHUB", "message_id.txt"),
        os.path.join(os.path.dirname(BASE_DIR), "message_id.txt"),
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
    path = resolve_msg_id_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            val = f.read().strip()
            return val or None
    return None


def save_message_id(msg_id: str) -> None:
    path = resolve_msg_id_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(msg_id).strip() + "\n")


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


def pick_working_thumbnail() -> str:
    for url in THUMBNAIL_CANDIDATES:
        try:
            r = requests.head(url, timeout=6, allow_redirects=True)
            if r.status_code == 200:
                return url
        except Exception:
            pass
    return THUMBNAIL_CANDIDATES[0]


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

            # Only grab explicitly server-scoped totals from server.html JSON (never global totals)
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


def extract_active_players_from_dom(page) -> tuple:
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
        res = page.evaluate(js)
        if res and res.get("foundSection"):
            if res.get("empty"):
                return [], "empty"
            return dedupe_player_entries(res.get("rows", [])), res.get("sampleRaw", "")
    except Exception:
        pass
    return [], ""


def looks_like_player_profile_json(obj) -> bool:
    """Detect if a JSON response is the wfservers.com player profile endpoint (so we can reuse it)."""
    if isinstance(obj, dict):
        lkeys = {str(k).lower() for k in obj.keys()}
        server_list_keys = {"servers", "server_history", "serverhistory", "server_stats", "serverstats", "history", "visits"}
        if lkeys & server_list_keys:
            return True
        if ("total_duration" in lkeys or "totalduration" in lkeys or "servers_visited" in lkeys):
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
    """Extract duration from a single per-server row object inside player profile JSON."""
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
    """
    Extract ONLY the player's duration on the target Get ReKt420 server from player profile JSON.
    Returns (profile_loaded: bool, server_duration_or_None).
    Never returns global all-server Total Duration.
    """
    exact_norm = normalize_name(server_name)
    base_norm = normalize_name(base_name)
    tier1_hits = []  # Exact server_id match
    tier2_hits = []  # Exact server_name match
    tier3_hits = []  # Prefix/base name (getrekt420) match
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

        # If inside a servers list, "id" and "name" may also represent the server
        if row_sname is None and isinstance(lmap.get("name"), str):
            if row_sid is not None or any(k in lmap for k in ("last_active", "lastactive", "last_seen", "lastseen", "duration", "duration_seconds", "hours")):
                row_sname = lmap["name"].strip()
        if row_sid is None and lmap.get("id") is not None and row_sname is not None:
            row_sid = str(lmap["id"]).strip()

        if row_sid is None and row_sname is None:
            return

        # Make sure this dict is actually a server visit row (has a duration field)
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


def extract_server_total_from_player_dom(page, server_id: str, server_name: str, base_name: str):
    """
    Read ONLY the target server's row from the Servers Visited table on wfservers.com/player.html.
    Explicitly ignores the site header/footer ('Cultivated by: Get ReKt420*™') and global Total Duration.
    """
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

      // Tier 1: Exact server.html?id=<serverId> link
      if (serverId) {
        const idRe = new RegExp('[?&]id=' + serverId + '(?:&|$)');
        for (const a of links) {
          const href = a.getAttribute('href') || '';
          if (idRe.test(href)) {
            const row = findRowForEl(a);
            const dur = extractDur(row.innerText || '');
            if (dur) return { loaded: true, duration: dur, matchType: 'dom_id' };
          }
        }
      }

      // Tier 2 & 3: Exact server name or base name (getrekt420) among server links
      let baseHit = null;
      for (const a of links) {
        const txtNorm = norm(a.innerText || '');
        if (!txtNorm) continue;
        const row = findRowForEl(a);
        const dur = extractDur(row.innerText || '');
        if (!dur) continue;
        if (exactNorm && txtNorm === exactNorm) {
          return { loaded: true, duration: dur, matchType: 'dom_exact_link' };
        }
        if (baseNorm && txtNorm.includes(baseNorm) && !baseHit) {
          baseHit = dur;
        }
      }
      if (baseHit) return { loaded: true, duration: baseHit, matchType: 'dom_base_link' };

      // Fallback: check table rows (in case server names aren't anchor links)
      const rows = Array.from(document.querySelectorAll('tbody tr, table tr')).filter(r => !r.querySelector('th') && !isBadContainer(r));
      if (rows.length > 0) hasAnyServerRows = true;
      for (const r of rows) {
        const t = r.innerText || '';
        const tn = norm(t);
        const dur = extractDur(t);
        if (!dur) continue;
        if (exactNorm && tn.includes(exactNorm)) {
          return { loaded: true, duration: dur, matchType: 'dom_exact_row' };
        }
        if (baseNorm && tn.includes(baseNorm) && !baseHit) {
          baseHit = dur;
        }
      }
      if (baseHit) return { loaded: true, duration: baseHit, matchType: 'dom_base_row' };

      return { loaded: hasAnyServerRows, duration: null, matchType: 'none' };
    }"""
    try:
        res = page.evaluate(
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
    """
    Text fallback: extract ONLY the target server's duration from the Servers Visited section of player.html.
    Explicitly ignores 'Cultivated by: Get ReKt420*™', global 'Total Duration', and AM/PM dates.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    exact_norm = normalize_name(server_name)
    base_norm = normalize_name(base_name)

    # Start scanning only after the profile summary metrics (e.g., 'Servers Visited' or 'Last Active')
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


def enrich_missing_totals_via_player_profiles(
    context, players: list, server_id: str, server_name: str, base_name: str
) -> None:
    """Pull each active player's Get ReKt420 server-specific Total Duration from wfservers.com/player.html."""
    missing = [p for p in players if parse_duration_seconds(p.get("total")) is None]
    if not missing:
        return

    print(f"Fetching {server_name} server-only totals for {len(missing)} active players...")

    def build_profile_url(p):
        if p.get("href"):
            return urljoin(TARGET_URL, p["href"])
        return f"https://www.wfservers.com/player.html?name={quote(p['name'], safe='')}"

    def block_heavy(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()

    first = missing[0]
    first_url = build_profile_url(first)
    discovered_api_template = None
    first_json_payloads = []
    probe_page = context.new_page()
    probe_page.route("**/*", block_heavy)

    first_name_enc = quote(first["name"], safe="")
    first_name_low = first["name"].lower()

    def on_probe_response(response):
        nonlocal discovered_api_template
        try:
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype.lower():
                return
            data = response.json()
            if looks_like_player_profile_json(data):
                first_json_payloads.append(data)
                u = response.url
                if first_name_enc in u:
                    discovered_api_template = u.replace(first_name_enc, "{NAME}")
                elif first_name_low in unquote(u).lower():
                    raw_dec = unquote(u)
                    idx = raw_dec.lower().find(first_name_low)
                    orig_slice = raw_dec[idx : idx + len(first["name"])]
                    if quote(orig_slice, safe="") in u:
                        discovered_api_template = u.replace(quote(orig_slice, safe=""), "{NAME}")
                    elif orig_slice in u:
                        discovered_api_template = u.replace(orig_slice, "{NAME}")
        except Exception:
            pass

    probe_page.on("response", on_probe_response)
    try:
        probe_page.goto(first_url, wait_until="networkidle", timeout=20000)
        try:
            probe_page.wait_for_function(
                "() => document.querySelectorAll('a[href*=\"server.html\"]').length > 0 || /servers\\s+visited/i.test(document.body.innerText)",
                timeout=7000,
            )
        except Exception:
            pass
        probe_page.wait_for_timeout(600)

        loaded_json, tot = extract_server_total_from_player_json(
            first_json_payloads, server_id, server_name, base_name
        )
        loaded_dom = False
        if tot is None:
            loaded_dom, tot = extract_server_total_from_player_dom(
                probe_page, server_id, server_name, base_name
            )
        if tot is None:
            tot = extract_server_total_from_player_text(
                probe_page.locator("body").inner_text(), server_name, base_name
            )
        if tot is not None:
            first["total"] = tot
        elif loaded_json or loaded_dom:
            # Profile loaded cleanly, player just has no prior banked time on this server yet
            first["total"] = 0
    except Exception as e:
        print(f"Profile probe error for {first['name']}: {e}")
    finally:
        probe_page.close()

    remaining = [p for p in missing[1:] if parse_duration_seconds(p.get("total")) is None]
    if not remaining:
        return

    # Fast path: reuse discovered player profile JSON API directly (~1-2s for all 24 players)
    if discovered_api_template:
        print("Using fast player API endpoint for server-specific totals...")
        for p in remaining:
            try:
                api_url = discovered_api_template.replace("{NAME}", quote(p["name"], safe=""))
                resp = requests.get(api_url, timeout=8)
                if resp.status_code == 200:
                    loaded, tot = extract_server_total_from_player_json(
                        [resp.json()], server_id, server_name, base_name
                    )
                    if tot is not None:
                        p["total"] = tot
                    elif loaded:
                        p["total"] = 0
            except Exception:
                pass

    # Fallback path: lightweight Playwright lookup for any remaining players
    still_missing = [p for p in remaining if parse_duration_seconds(p.get("total")) is None]
    if still_missing:
        page = context.new_page()
        page.route("**/*", block_heavy)
        try:
            for p in still_missing:
                try:
                    p_json = []

                    def capture_p(resp, bucket=p_json):
                        try:
                            if "json" in resp.headers.get("content-type", "").lower():
                                bucket.append(resp.json())
                        except Exception:
                            pass

                    page.on("response", capture_p)
                    page.goto(build_profile_url(p), wait_until="domcontentloaded", timeout=15000)
                    try:
                        page.wait_for_function(
                            "() => document.querySelectorAll('a[href*=\"server.html\"]').length > 0 || /last\\s+active/i.test(document.body.innerText)",
                            timeout=6000,
                        )
                    except Exception:
                        pass
                    page.wait_for_timeout(300)
                    page.remove_listener("response", capture_p)

                    loaded_j, tot = extract_server_total_from_player_json(
                        p_json, server_id, server_name, base_name
                    )
                    loaded_d = False
                    if tot is None:
                        loaded_d, tot = extract_server_total_from_player_dom(
                            page, server_id, server_name, base_name
                        )
                    if tot is None:
                        tot = extract_server_total_from_player_text(
                            page.locator("body").inner_text(), server_name, base_name
                        )
                    if tot is not None:
                        p["total"] = tot
                    elif loaded_j or loaded_d:
                        p["total"] = 0
                except Exception:
                    pass
        finally:
            page.close()


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


def find_server_page_url(page, target_norm: str) -> str:
    if SERVER_PAGE_URL.strip():
        return SERVER_PAGE_URL.strip()

    page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)

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
        href = page.evaluate(js, target_norm)
        if href:
            return urljoin(TARGET_URL, href)
    except Exception:
        pass

    return FALLBACK_SERVER_URL


def fetch_server_status() -> dict:
    target_norm = normalize_name(SERVER_NAME)
    captured_json = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        server_url = find_server_page_url(page, target_norm)
        server_id = extract_server_id(server_url)

        def handle_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype.lower():
                    captured_json.append(response.json())
            except Exception:
                pass

        page.on("response", handle_response)
        page.goto(server_url, wait_until="networkidle", timeout=45000)

        try:
            page.wait_for_function(
                "() => !document.body.innerText.includes('LOADING SERVER TELEMETRY')",
                timeout=15000,
            )
        except Exception:
            pass
        page.wait_for_timeout(4000)

        dom_players, sample_raw = extract_active_players_from_dom(page)
        full_text = page.locator("body").inner_text()

        parsed = extract_detail_fields_from_text(full_text)
        json_players = extract_active_players_from_json(captured_json)
        leaders_map = extract_duration_leaders_map(full_text)

        if sample_raw:
            print(f"Row 1 sample: {sample_raw}")

        if dom_players:
            json_by_name = {p["name"].lower(): p for p in json_players}
            players = []
            for p in dom_players:
                low = p["name"].lower()
                jp = json_by_name.get(low, {})
                session = p["session"] if p["session"] is not None else jp.get("session")
                total = p["total"] if p["total"] is not None else jp.get("total")
                players.append({
                    "name": p["name"],
                    "href": p.get("href"),
                    "session": session,
                    "total": total,
                    "session_secs": parse_duration_seconds(session),
                })
        else:
            players = []
            for p in json_players:
                players.append({
                    "name": p["name"],
                    "href": p.get("href"),
                    "session": p["session"],
                    "total": p["total"],
                    "session_secs": parse_duration_seconds(p["session"]),
                })

        if any(p["session_secs"] is not None for p in players):
            players.sort(
                key=lambda p: (p["session_secs"] if p["session_secs"] is not None else -1),
                reverse=True,
            )

        players = players[:MAX_PLAYERS_SHOWN]

        display_server_name = parsed["name"] or SERVER_NAME
        if players and any(parse_duration_seconds(pl.get("total")) is None for pl in players):
            enrich_missing_totals_via_player_profiles(
                context, players, server_id, display_server_name, SERVER_NAME
            )

        # Secondary fallback: Duration Leaders Top 20 on this exact server page
        for pl in players:
            if parse_duration_seconds(pl.get("total")) is None:
                fallback_tot = leaders_map.get(pl["name"].lower())
                if fallback_tot is not None:
                    pl["total"] = fallback_tot

        browser.close()

    current = parsed["current"] if parsed["current"] is not None else len(players)
    maximum = parsed["maximum"] if parsed["maximum"] is not None else 24

    if current == 0:
        players = []

    found = parsed["online"] is not False and (parsed["online"] is True or parsed["maximum"] is not None)

    return {
        "found": found,
        "server_name": display_server_name,
        "server_url": server_url,
        "current": current,
        "maximum": maximum,
        "players": players,
    }


def build_ansi_roster_table(players: list) -> str:
    ESC = "\u001b"
    RESET = f"{ESC}[0m"
    HEADER = f"{ESC}[1;37m"                         # Bold white header
    YELLOW_1ST = f"{ESC}[1;33m{ESC}[38;2;220;178;60m"    # 1st place: Gold/Yellow text (no background box)
    GRAY_2ND = f"{ESC}[1;37m{ESC}[38;2;152;158;168m"     # 2nd place: Silver/Gray text (no background box)
    ORANGE_3RD = f"{ESC}[1;31m{ESC}[38;2;215;118;62m"    # 3rd place: Bronze/Orange text (no background box)
    LEGIBLE_RANK = f"{ESC}[0;37m"                        # 4th-24th: White matching player names
    ROW_TEXT = f"{ESC}[0;37m"                            # White text for names, session, total
    DIVIDER = f"{ESC}[0;37m"

    # Total visible width = 4 + 1 + 16 + 1 + 8 + 1 + 9 = 40 chars (never wraps in Discord embeds)
    header = f"{HEADER}{'Rank':<4} {'Player':<16} {'Session':>8} {'Total':>9}{RESET}"
    divider = f"{DIVIDER}{'─' * 40}{RESET}"
    lines = [header, divider]

    if not players:
        lines.append(f"{ROW_TEXT}   No active players online right now{RESET}")
    else:
        for rank, p in enumerate(players[:MAX_PLAYERS_SHOWN], start=1):
            raw_badge = f"[{rank}]"
            left_pad = " " * (4 - len(raw_badge))
            if rank == 1:
                badge = f"{left_pad}{YELLOW_1ST}{raw_badge}{RESET}"
            elif rank == 2:
                badge = f"{left_pad}{GRAY_2ND}{raw_badge}{RESET}"
            elif rank == 3:
                badge = f"{left_pad}{ORANGE_3RD}{raw_badge}{RESET}"
            else:
                badge = f"{left_pad}{LEGIBLE_RANK}{raw_badge}{RESET}"

            raw_name = p["name"].replace("`", "'")
            if len(raw_name) > 16:
                display_name = raw_name[:15] + "…"
            else:
                display_name = raw_name

            sess = format_session_time(p.get("session"))
            tot = format_total_time(p.get("total"))
            lines.append(f"{badge} {ROW_TEXT}{display_name:<16} {sess:>8} {tot:>9}{RESET}")

    return "```ansi\n" + "\n".join(lines) + "\n```"


def build_embed(info: dict, thumbnail_url: str = None) -> dict:
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

    roster_heading = f"**Active Players ({current}/{maximum})**" if maximum else f"**Active Players ({current})**"
    table_block = build_ansi_roster_table(players)

    desc_parts = [f"**{status_line}**"]
    if bar:
        desc_parts.append(bar)
    desc_parts.append("")
    desc_parts.append(roster_heading)
    desc_parts.append(table_block)
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

    return {"embeds": [embed]}


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not is_valid_webhook(webhook_url):
        print("ERROR: Missing or invalid DISCORD_WEBHOOK_URL secret.")
        sys.exit(1)

    info = fetch_server_status()
    thumb = pick_working_thumbnail()
    payload = build_embed(info, thumbnail_url=thumb)
    message_id = load_message_id()

    if message_id:
        res = requests.patch(f"{webhook_url}/messages/{message_id}", json=payload, timeout=20)
        if res.status_code == 404:
            print("Previous Discord card was deleted. Posting a new one...")
            message_id = None
        else:
            res.raise_for_status()
            with_totals = sum(1 for pl in info["players"] if parse_duration_seconds(pl.get("total")) is not None)
            print(
                f"Updated card: {info['current']}/{info['maximum']} players "
                f"({len(info['players'])} listed, {with_totals} with Get ReKt420 Total)"
            )

    if not message_id:
        res = requests.post(f"{webhook_url}?wait=true", json=payload, timeout=20)
        res.raise_for_status()
        new_id = res.json()["id"]
        save_message_id(new_id)
        print(f"Posted new live Discord card! Message ID saved: {new_id}")


if __name__ == "__main__":
    main()
