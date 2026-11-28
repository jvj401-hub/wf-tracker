import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin
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
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val < 0:
            return None
        return int(round(val))
    s = str(val).strip()
    if not s or s in ("—", "-", "--:--", "--:--:--"):
        return None
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.match(r"^(\d+):(\d{2})$", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    # Hours/minutes/seconds text like "103 hrs", "18.5h", "2h 15m", "45m"
    total = 0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hrs?|hours?|h|mins?|minutes?|m|secs?|seconds?|s)\b", s, re.I):
        matched = True
        v = float(num)
        u = unit.lower()
        if u.startswith("h"):
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
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_total_time(val) -> str:
    if isinstance(val, str):
        s = val.strip()
        m = re.match(r"^(\d+):(\d{2})(?::\d{2})?$", s)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    secs = parse_duration_seconds(val)
    if secs is None:
        return "--:--"
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h:02d}:{m:02d}"


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
            "session": item.get("session"),
            "total": item.get("total"),
            "session_secs": parse_duration_seconds(item.get("session")),
        })
    return clean


def extract_players_from_list(items) -> list:
    entries = []
    if not isinstance(items, list):
        return entries
    for p in items:
        if isinstance(p, str):
            entries.append({"name": p, "session": None, "total": None})
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
            for key in (
                "session", "session_time", "sessiontime", "session_duration",
                "duration", "time", "connected", "online_time", "playtime", "current_session",
            ):
                if key in pmap and pmap[key] is not None:
                    session_val = pmap[key]
                    break

            total_val = None
            for key in (
                "total", "total_time", "totaltime", "total_duration", "total_hours",
                "cumulative", "all_time", "alltime", "lifetime", "hours", "total_playtime",
            ):
                if key in pmap and pmap[key] is not None:
                    v = pmap[key]
                    # If field name implies hours and value is numeric, convert to seconds
                    if isinstance(v, (int, float)) and "hour" in key:
                        total_val = int(round(v * 3600))
                    else:
                        total_val = v
                    break

            entries.append({"name": name, "session": session_val, "total": total_val})
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

    end = min(len(lines), start + 140)
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


def extract_active_players_from_dom(page) -> list:
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

      if (!bestContainer) return { foundSection: false, empty: false, rows: [] };

      const containerText = (bestContainer.innerText || '');
      if (/no\s+active\s+players/i.test(containerText)) {
        return { foundSection: true, empty: true, rows: [] };
      }

      const timeRe = /\b\d+:\d{2}(?::\d{2})?\b|\b\d+(?:\.\d+)?\s*(?:hrs?|hours?|mins?|minutes?|secs?|seconds?)\b/gi;
      const ignoredNameRe = /^(active\s+players.*|no\s+active\s+players.*|rank|player|player\s+name|driver|name|session|total|duration|score|ping|time|#|\d+)$/i;

      const parseRowElement = (rowEl) => {
        const rawText = (rowEl.innerText || '').trim();
        if (!rawText) return null;

        let name = '';
        const link = rowEl.querySelector('a[href*="player"]');
        if (link && (link.innerText || '').trim()) {
          name = (link.innerText || '').trim();
        }

        const times = rawText.match(timeRe) || [];

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
          session: times.length >= 1 ? times[0] : null,
          total: times.length >= 2 ? times[1] : null,
        };
      };

      // 1. Try structured table rows or list items inside the Active Players container
      const rowEls = Array.from(bestContainer.querySelectorAll('tbody tr, tr, li, [class*="player-row"], [class*="player_row"], [class*="row"]'));
      const parsedRows = [];
      for (const r of rowEls) {
        if (r.querySelector('th')) continue;
        const item = parseRowElement(r);
        if (item) parsedRows.push(item);
      }
      if (parsedRows.length > 0) {
        return { foundSection: true, empty: false, rows: parsedRows };
      }

      // 2. Fallback: find each player link and read its closest row container
      const links = Array.from(bestContainer.querySelectorAll('a[href*="player"]'));
      for (const a of links) {
        const parent = a.closest('tr, li, div') || a;
        const item = parseRowElement(parent);
        if (item) parsedRows.push(item);
      }
      if (parsedRows.length > 0) {
        return { foundSection: true, empty: false, rows: parsedRows };
      }

      // 3. Last-resort line scan inside the Active Players box
      const lines = containerText.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        if (ignoredNameRe.test(line)) continue;
        const times = line.match(timeRe) || [];
        let cleaned = line.replace(timeRe, '').replace(/^\s*\d+\s+/, '').trim();
        if (cleaned && !ignoredNameRe.test(cleaned)) {
          parsedRows.push({
            name: cleaned,
            session: times.length >= 1 ? times[0] : null,
            total: times.length >= 2 ? times[1] : null,
          });
        }
      }
      return { foundSection: true, empty: false, rows: parsedRows };
    }"""
    try:
        res = page.evaluate(js)
        if res and res.get("foundSection"):
            if res.get("empty"):
                return []
            return dedupe_player_entries(res.get("rows", []))
    except Exception:
        pass
    return []


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
        page = browser.new_page()

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
        page.wait_for_timeout(2500)

        dom_players = extract_active_players_from_dom(page)
        full_text = page.locator("body").inner_text()
        browser.close()

    parsed = extract_detail_fields_from_text(full_text)
    json_players = extract_active_players_from_json(captured_json)
    leaders_map = extract_duration_leaders_map(full_text)

    # Merge DOM and JSON players (DOM preserves exact on-page table order; JSON can fill missing times)
    if dom_players:
        json_by_name = {p["name"].lower(): p for p in json_players}
        players = []
        for p in dom_players:
            jp = json_by_name.get(p["name"].lower(), {})
            session = p["session"] if p["session"] is not None else jp.get("session")
            total = p["total"] if p["total"] is not None else jp.get("total")
            if total is None:
                total = leaders_map.get(p["name"].lower())
            players.append({
                "name": p["name"],
                "session": session,
                "total": total,
                "session_secs": parse_duration_seconds(session),
            })
    else:
        players = []
        for p in json_players:
            total = p["total"] if p["total"] is not None else leaders_map.get(p["name"].lower())
            players.append({
                "name": p["name"],
                "session": p["session"],
                "total": total,
                "session_secs": parse_duration_seconds(p["session"]),
            })

    # Sort descending by session time if session times were extracted
    if any(p["session_secs"] is not None for p in players):
        players.sort(key=lambda p: (p["session_secs"] if p["session_secs"] is not None else -1), reverse=True)

    current = parsed["current"] if parsed["current"] is not None else len(players)
    maximum = parsed["maximum"] if parsed["maximum"] is not None else 24

    if current == 0:
        players = []

    found = parsed["online"] is not False and (parsed["online"] is True or parsed["maximum"] is not None)

    return {
        "found": found,
        "server_name": parsed["name"] or SERVER_NAME,
        "server_url": server_url,
        "current": current,
        "maximum": maximum,
        "players": players[:MAX_PLAYERS_SHOWN],
    }


def build_ansi_roster_table(players: list) -> str:
    ESC = "\u001b"
    RESET = f"{ESC}[0m"
    GOLD = f"{ESC}[1;33m"
    SILVER = f"{ESC}[1;37m"
    BRONZE = f"{ESC}[1;31m"
    DIM = f"{ESC}[1;30m"
    WHITE = f"{ESC}[0;37m"

    header = f"{WHITE}{'Rank':<4}  {'Player Name':<20}  {'Session':>8}  {'Total':>7}{RESET}"
    divider = f"{DIM}{'─' * 45}{RESET}"
    lines = [header, divider]

    if not players:
        lines.append(f"{DIM}      No active players online right now{RESET}")
    else:
        for rank, p in enumerate(players[:MAX_PLAYERS_SHOWN], start=1):
            badge_raw = f"[{rank}]"
            badge_padded = f"{badge_raw:>4}"
            if rank == 1:
                badge = f"{GOLD}{badge_padded}{RESET}"
            elif rank == 2:
                badge = f"{SILVER}{badge_padded}{RESET}"
            elif rank == 3:
                badge = f"{BRONZE}{badge_padded}{RESET}"
            else:
                badge = f"{DIM}{badge_padded}{RESET}"

            # Strip backticks so player tags cannot break the Discord code block
            raw_name = p["name"].replace("`", "'")
            if len(raw_name) > 20:
                display_name = raw_name[:19] + "…"
            else:
                display_name = raw_name

            sess = format_session_time(p.get("session"))
            tot = format_total_time(p.get("total"))
            lines.append(f"{badge}  {WHITE}{display_name:<20}  {sess:>8}  {tot:>7}{RESET}")

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
            print(f"Updated card: {info['current']}/{info['maximum']} players ({len(info['players'])} listed)")

    if not message_id:
        res = requests.post(f"{webhook_url}?wait=true", json=payload, timeout=20)
        res.raise_for_status()
        new_id = res.json()["id"]
        save_message_id(new_id)
        print(f"Posted new live Discord card! Message ID saved: {new_id}")


if __name__ == "__main__":
    main()
