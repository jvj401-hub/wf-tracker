import os
import re
import sys
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

SERVER_NAME = "Get ReKt420"
TARGET_URL = "https://wfservers.com"
MSG_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message_id.txt")


def is_valid_webhook(url: str) -> bool:
    url = (url or "").strip()
    return (
        url.startswith("https://discord.com/api/webhooks/")
        or url.startswith("https://discordapp.com/api/webhooks/")
        or url.startswith("https://ptb.discord.com/api/webhooks/")
        or url.startswith("https://canary.discord.com/api/webhooks/")
    )


def load_message_id():
    if os.path.exists(MSG_ID_FILE):
        with open(MSG_ID_FILE, "r", encoding="utf-8") as f:
            val = f.read().strip()
            return val or None
    return None


def save_message_id(msg_id: str) -> None:
    with open(MSG_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(msg_id).strip() + "\n")


def normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def make_progress_bar(current: int, maximum: int, length: int = 16) -> str:
    if not maximum or maximum <= 0:
        return ""
    pct = min(max(current / maximum, 0.0), 1.0)
    filled = round(pct * length)
    return f"`[{'■' * filled}{'□' * (length - filled)}]` {int(pct * 100)}%"


def find_server_in_json(obj, target_norm):
    if isinstance(obj, dict):
        for val in obj.values():
            if isinstance(val, str) and target_norm in normalize_name(val):
                return obj
        for val in obj.values():
            res = find_server_in_json(val, target_norm)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_server_in_json(item, target_norm)
            if res is not None:
                return res
    return None


def extract_from_json_entry(entry: dict):
    lower_map = {str(k).lower(): v for k, v in entry.items()}

    def pick_int(keys):
        for k in keys:
            if k in lower_map and lower_map[k] is not None:
                try:
                    return int(lower_map[k])
                except (ValueError, TypeError):
                    pass
        return None

    def pick_str(keys):
        for k in keys:
            val = lower_map.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    current = pick_int([
        "players", "player_count", "playercount", "currentplayers",
        "numplayers", "num_players", "clients",
    ])
    maximum = pick_int([
        "maxplayers", "max_players", "maxclients", "max_clients",
        "slots", "capacity",
    ])

    if current is None and isinstance(lower_map.get("players"), list):
        current = len(lower_map["players"])

    track = pick_str(["track", "map", "trackname", "track_name", "current_track", "mapname"])
    mode = pick_str(["gamemode", "game_mode", "mode", "event", "event_type", "class"])
    region = pick_str(["region", "country", "location"])
    ping = pick_int(["ping", "latency"])

    player_names = []
    for k in ["playerlist", "player_list", "players", "roster"]:
        val = lower_map.get(k)
        if isinstance(val, list) and val:
            for p in val:
                if isinstance(p, str):
                    player_names.append(p)
                elif isinstance(p, dict):
                    pname = p.get("name") or p.get("username") or p.get("player")
                    if pname:
                        player_names.append(str(pname))
            if player_names:
                break

    if current is not None or maximum is not None:
        return {
            "found": True,
            "current": current or 0,
            "maximum": maximum or 24,
            "track": track,
            "mode": mode,
            "region": region,
            "ping": f"{ping}ms" if ping is not None else None,
            "players": player_names,
            "details": None,
        }
    return None


def fetch_server_status():
    target_norm = normalize_name(SERVER_NAME)
    captured_json = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def handle_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype.lower():
                    captured_json.append(response.json())
            except Exception:
                pass

        page.on("response", handle_response)
        page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3500)

        rows = page.locator("tr, [class*='server'], [class*='row'], [class*='card'], li").all_inner_texts()
        full_text = page.locator("body").inner_text()
        browser.close()

    # 1. Try clean JSON payload if wfservers.com loads data via an API
    for payload in captured_json:
        entry = find_server_in_json(payload, target_norm)
        if entry:
            parsed = extract_from_json_entry(entry)
            if parsed:
                return parsed

    # 2. Try matching specific HTML row/card elements
    candidates = []
    for row in rows:
        if target_norm in normalize_name(row):
            match = re.search(r"(\d+)\s*/\s*(\d+)", row)
            if match:
                candidates.append((len(row), int(match.group(1)), int(match.group(2)), row.strip()))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        _, current, maximum, details = candidates[0]
        return {
            "found": True,
            "current": current,
            "maximum": maximum,
            "track": None,
            "mode": None,
            "region": None,
            "ping": None,
            "players": [],
            "details": details,
        }

    # 3. Fallback: scan full page text lines near the server name
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if target_norm in normalize_name(line):
            window = " | ".join(lines[idx : idx + 6])
            match = re.search(r"(\d+)\s*/\s*(\d+)", window)
            if match:
                return {
                    "found": True,
                    "current": int(match.group(1)),
                    "maximum": int(match.group(2)),
                    "track": None,
                    "mode": None,
                    "region": None,
                    "ping": None,
                    "players": [],
                    "details": window,
                }

    return {
        "found": False,
        "current": 0,
        "maximum": 0,
        "track": None,
        "mode": None,
        "region": None,
        "ping": None,
        "players": [],
        "details": "Server not currently listed on wfservers.com.",
    }


def build_embed(info: dict) -> dict:
    found = info["found"]
    current = info["current"]
    maximum = info["maximum"]

    if not found:
        color = 0x95A5A6
        header = "⚪ Offline / Not Listed"
        state = "Offline"
        bar = ""
    elif current > 0:
        color = 0x2ECC71
        header = f"🟢 Online — {current} / {maximum} Players"
        state = "🟢 Active"
        bar = make_progress_bar(current, maximum)
    else:
        color = 0xE67E22
        header = f"🟠 Empty — 0 / {maximum} Players"
        state = "🟠 Waiting for Players"
        bar = make_progress_bar(0, maximum)

    description = f"**{header}**" + (f"\n{bar}" if bar else "")

    fields = [
        {"name": "Status", "value": state, "inline": True},
        {"name": "Players", "value": f"{current} / {maximum}" if maximum else "N/A", "inline": True},
    ]

    if info.get("track"):
        fields.append({"name": "Current Track", "value": info["track"], "inline": True})
    if info.get("mode"):
        fields.append({"name": "Event Type", "value": info["mode"], "inline": True})
    if info.get("region"):
        fields.append({"name": "Region", "value": info["region"], "inline": True})
    if info.get("ping"):
        fields.append({"name": "Ping", "value": info["ping"], "inline": True})

    if info.get("players"):
        shown = info["players"][:12]
        extra = len(info["players"]) - len(shown)
        roster = ", ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")
        fields.append({"name": f"Active Players ({current}/{maximum})", "value": roster[:1000], "inline": False})
    elif info.get("details"):
        clean = " | ".join(line.strip() for line in info["details"].splitlines() if line.strip())
        fields.append({"name": "Live Details", "value": f"`{clean[:300]}`", "inline": False})

    return {
        "embeds": [
            {
                "author": {"name": "wfservers.com — Server Tracker"},
                "title": f"Wreckfest — {SERVER_NAME}",
                "url": TARGET_URL,
                "description": description,
                "color": color,
                "fields": fields,
                "footer": {"text": "Source: wfservers.com • 24/7 Cloud Auto-Update"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not is_valid_webhook(webhook_url):
        print("ERROR: Missing or invalid DISCORD_WEBHOOK_URL secret.")
        print("In GitHub, go to Settings -> Secrets and variables -> Actions -> New repository secret.")
        print("Set Name to DISCORD_WEBHOOK_URL and paste your Discord Webhook URL as the Secret value.")
        sys.exit(1)

    info = fetch_server_status()
    payload = build_embed(info)
    message_id = load_message_id()

    if message_id:
        res = requests.patch(f"{webhook_url}/messages/{message_id}", json=payload, timeout=20)
        if res.status_code == 404:
            print("Previous Discord card was deleted. Posting a new one...")
            message_id = None
        else:
            res.raise_for_status()
            print(f"Updated live Discord card: {info['current']}/{info['maximum']} players")

    if not message_id:
        res = requests.post(f"{webhook_url}?wait=true", json=payload, timeout=20)
        res.raise_for_status()
        new_id = res.json()["id"]
        save_message_id(new_id)
        print(f"Posted new live Discord card! Message ID saved: {new_id}")


if __name__ == "__main__":
    main()
