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
    # Text like "103 hrs", "18.5h", "2d 4h 10m", "45m"
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
            total_keys = (
                "total", "total_duration", "totalduration", "total_time", "totaltime",
                "total_seconds", "totalseconds", "total_sec", "totalsec",
                "total_duration_seconds", "totaldurationseconds", "total_time_seconds",
                "total_hours", "totalhours", "total_minutes", "totalminutes", "total_ms", "totalms",
                "server_duration", "serverduration", "server_time", "servertime",
                "server_seconds", "serverseconds", "server_hours", "serverhours",
                "cumulative", "cumulative_duration", "cumulative_seconds", "cumulative_hours",
                "all_time", "alltime", "all_time_duration", "alltimeduration",
                "overall_duration", "overallduration", "lifetime", "lifetime_duration",
                "total_playtime", "totalplaytime", "playtime", "hours",
            )
            for key in total_keys:
                if key in pmap and pmap[key] is not None:
                    total_val = convert_numeric_duration_by_key(key, pmap[key])
                    break

            # Fuzzy fallback for any key mentioning total/cumulative/overall/alltime + time/dur/sec/hour
            if total_val is None:
                for key, val in pmap.items():
                    if val is None or isinstance(val, (dict, list, bool)):
                        continue
                    if any(tok in key for tok in ("total", "cumul", "overall", "alltime", "all_time", "lifetime")):
                        if any(tok in key for tok in ("dur", "time", "sec", "hour", "hr", "min", "play")):
                            cand = convert_numeric_duration_by_key(key, val)
                            if cand is not None:
                                total_val = cand
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


def collect_player_totals_from_all_json(payloads) -> dict:
    """Scan every captured JSON response for name -> total duration pairs."""
    totals = {}

    def walk(obj):
        if isinstance(obj, dict):
            lmap = {str(k).lower(): v for k, v in obj.items()}
            name = None
            for k in ("name", "player_name", "playername", "username", "player", "driver", "nick"):
                v = lmap.get(k)
                if isinstance(v, str) and v.strip():
                    name = v.strip()
                    break
            if name:
                for k, v in lmap.items():
                    if v is None or isinstance(v, (dict, list, bool)):
                        continue
                    if any(tok in k for tok in ("total", "cumul", "overall", "alltime", "all_time", "lifetime", "server_dur", "serverdur")):
                        if any(tok in k for tok in ("dur", "time", "sec", "hour", "hr", "min", "play")) or k == "total":
                            cand = convert_numeric_duration_by_key(k, v)
                            if parse_duration_seconds(cand) is not None:
                                totals[name.lower()] = cand
                                break
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for payload in payloads:
        walk(payload)
    return totals


def extract_duration_leaders_map(full_text: str) -> dict:
    """Fallback lookup of cumulative hours from the Duration Leaders table on the same page."""
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

        // Read cell-by-cell first so separate columns never merge awkwardly
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

      // 1. Prefer real table rows (tbody tr) so we always capture ALL columns of each row
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

      // 2. Fallback: find player links and walk up to the full row container
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

      // 3. Last-resort line scan inside the Active Players container
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


def extract_total_duration_from_player_text(text: str, server_name: str = None):
    """Extract Total Duration from a wfservers.com/player.html profile page."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    # Primary: "Total Duration" metric card near the top of player.html (e.g. "521:24:34")
    for i, ln in enumerate(lines):
        if re.search(r"total\s+duration", ln, re.I):
            # Check same line first
            m_same = re.search(r"(\d+:\d{2}:\d{2})", ln)
            if m_same:
                return m_same.group(1)
            # Check next 3 lines
            for nxt in lines[i + 1 : i + 4]:
                m = re.search(r"(\d+:\d{2}:\d{2})", nxt)
                if m:
                    return m.group(1)
                m_hrs = re.search(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\b", nxt, re.I)
                if m_hrs:
                    return int(round(float(m_hrs.group(1)) * 3600))

    # Secondary: look for the server name row on the player's profile page
    if server_name:
        snorm = normalize_name(server_name)
        for i, ln in enumerate(lines):
            if snorm and snorm in normalize_name(ln):
                for nxt in lines[i : i + 4]:
                    m = re.search(r"(\d+:\d{2}:\d{2})", nxt)
                    if m:
                        return m.group(1)
    return None


def extract_total_from_player_json(payloads):
    """Extract total duration value from a player profile JSON response."""
    def walk(obj):
        if isinstance(obj, dict):
            lmap = {str(k).lower(): v for k, v in obj.items()}
            for k in (
                "total_duration", "totalduration", "total_duration_seconds", "totaldurationseconds",
                "total_seconds", "totalseconds", "total_time", "totaltime",
                "duration_seconds", "durationseconds", "total_hours", "totalhours",
            ):
                if k in lmap and lmap[k] is not None:
                    cand = convert_numeric_duration_by_key(k, lmap[k])
                    if parse_duration_seconds(cand) is not None:
                        return cand
            for v in obj.values():
                res = walk(v)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = walk(item)
                if res is not None:
                    return res
        return None

    for p in payloads:
        res = walk(p)
        if res is not None:
            return res
    return None


def enrich_missing_totals_via_player_profiles(context, players: list, server_name: str) -> None:
    """If server.html did not expose Total Duration, pull each active player's Total Duration from wfservers.com/player.html."""
    missing = [p for p in players if parse_duration_seconds(p.get("total")) is None]
    if not missing:
        return

    print(f"Fetching Total Duration for {len(missing)} active players from player profiles...")

    def build_profile_url(p):
        if p.get("href"):
            return urljoin(TARGET_URL, p["href"])
        return f"https://www.wfservers.com/player.html?name={quote(p['name'], safe='')}"

    # Block heavy assets so player profile fetches finish in ~0.5-1s each
    def block_heavy(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()

    # Probe the first missing player and watch for a reusable JSON API endpoint
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
            tot = extract_total_from_player_json([data])
            if tot is not None:
                first_json_payloads.append(data)
                u = response.url
                if first_name_enc in u:
                    discovered_api_template = u.replace(first_name_enc, "{NAME}")
                elif first_name_low in unquote(u).lower():
                    idx = unquote(u).lower().find(first_name_low)
                    raw_dec = unquote(u)
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
                "() => /\\d+:\\d{2}:\\d{2}/.test(document.body.innerText)",
                timeout=6000,
            )
        except Exception:
            pass
        probe_page.wait_for_timeout(500)
        tot = extract_total_from_player_json(first_json_payloads)
        if tot is None:
            tot = extract_total_duration_from_player_text(probe_page.locator("body").inner_text(), server_name)
        if tot is not None:
            first["total"] = tot
    except Exception as e:
        print(f"Profile probe error for {first['name']}: {e}")
    finally:
        probe_page.close()

    remaining = [p for p in missing[1:] if parse_duration_seconds(p.get("total")) is None]
    if not remaining:
        return

    # Fast path: reuse discovered JSON API endpoint directly for all remaining players (~1-2s total)
    if discovered_api_template:
        print("Using fast player API endpoint for remaining players...")
        for p in remaining:
            try:
                api_url = discovered_api_template.replace("{NAME}", quote(p["name"], safe=""))
                resp = requests.get(api_url, timeout=8)
                if resp.status_code == 200:
                    tot = extract_total_from_player_json([resp.json()])
                    if tot is not None:
                        p["total"] = tot
            except Exception:
                pass

    # Fallback path: fetch any still-missing player pages with a lightweight Playwright page
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
                            "() => /\\d+:\\d{2}:\\d{2}/.test(document.body.innerText)",
                            timeout=5000,
                        )
                    except Exception:
                        pass
                    page.remove_listener("response", capture_p)
                    tot = extract_total_from_player_json(p_json)
                    if tot is None:
                        tot = extract_total_duration_from_player_text(page.locator("body").inner_text(), server_name)
                    if tot is not None:
                        p["total"] = tot
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
        # Give async total-duration cells time to finish hydrating on server.html
        page.wait_for_timeout(4000)

        dom_players, sample_raw = extract_active_players_from_dom(page)
        full_text = page.locator("body").inner_text()

        parsed = extract_detail_fields_from_text(full_text)
        json_players = extract_active_players_from_json(captured_json)
        json_totals_map = collect_player_totals_from_all_json(captured_json)
        leaders_map = extract_duration_leaders_map(full_text)

        if sample_raw:
            print(f"Row 1 sample: {sample_raw}")

        # Merge DOM + JSON + Duration Leaders
        if dom_players:
            json_by_name = {p["name"].lower(): p for p in json_players}
            players = []
            for p in dom_players:
                low = p["name"].lower()
                jp = json_by_name.get(low, {})
                session = p["session"] if p["session"] is not None else jp.get("session")
                total = p["total"]
                if total is None:
                    total = jp.get("total")
                if total is None:
                    total = json_totals_map.get(low)
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
                low = p["name"].lower()
                total = p["total"] if p["total"] is not None else json_totals_map.get(low)
                players.append({
                    "name": p["name"],
                    "href": p.get("href"),
                    "session": p["session"],
                    "total": total,
                    "session_secs": parse_duration_seconds(p["session"]),
                })

        if any(p["session_secs"] is not None for p in players):
            players.sort(
                key=lambda p: (p["session_secs"] if p["session_secs"] is not None else -1),
                reverse=True,
            )

        players = players[:MAX_PLAYERS_SHOWN]

        # If server.html didn't include Total Duration for active players, look them up on wfservers.com/player.html
        display_server_name = parsed["name"] or SERVER_NAME
        if players and any(parse_duration_seconds(pl.get("total")) is None for pl in players):
            enrich_missing_totals_via_player_profiles(context, players, display_server_name)

        # Last fallback: Duration Leaders Top 20 on server.html
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
    HEADER = f"{ESC}[1;37m"      # Crisp bold white header
    YELLOW = f"{ESC}[1;33m"      # 1st place: Yellow
    GRAY_2ND = f"{ESC}[1;37;42m" # 2nd place: Slate Gray badge
    ORANGE_3RD = f"{ESC}[1;37;41m"  # 3rd place: Orange badge (#cb4b16 in Discord ANSI)
    LEGIBLE_RANK = f"{ESC}[0;37m"   # 4th-24th: Clean readable light gray
    ROW_TEXT = f"{ESC}[0;37m"       # Clean readable light gray text
    DIVIDER = f"{ESC}[0;37m"        # Subtle, non-wrapping divider

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
                badge = f"{left_pad}{YELLOW}{raw_badge}{RESET}"
            elif rank == 2:
                badge = f"{left_pad}{GRAY_2ND}{raw_badge}{RESET}"
            elif rank == 3:
                badge = f"{left_pad}{ORANGE_3RD}{raw_badge}{RESET}"
            else:
                badge = f"{left_pad}{LEGIBLE_RANK}{raw_badge}{RESET}"

            # Strip backticks so player tags cannot break the Discord code block
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
                f"({len(info['players'])} listed, {with_totals} with Total Duration)"
            )

    if not message_id:
        res = requests.post(f"{webhook_url}?wait=true", json=payload, timeout=20)
        res.raise_for_status()
        new_id = res.json()["id"]
        save_message_id(new_id)
        print(f"Posted new live Discord card! Message ID saved: {new_id}")


if __name__ == "__main__":
    main()
