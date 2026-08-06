#!/usr/bin/env python3
"""
Kobe 5 / Kobe 6 restock + shock drop monitor.

Watches Nike's public product feed (the same data SNKRS and nike.com render from)
and optionally a set of retailer product pages, then pushes an instant phone
notification via ntfy.sh when something goes live, restocks, or adds sizes.

This is an ALERT tool only. It does not add to cart, does not check out, and does
not touch your account. You still buy manually like everyone else.

Usage:
    export NTFY_TOPIC="kobe-alerts-3f9a2c"     # pick something unguessable
    python kobe_monitor.py

Optional env:
    NTFY_SERVER   default https://ntfy.sh
    PROXY_URL     http://user:pass@ip:port   (only needed if Nike starts 403ing you)
    POLL_SECONDS  default 8
"""

import asyncio
import json
import os
import random
import time
from pathlib import Path

import aiohttp

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
PROXY_URL = os.getenv("PROXY_URL") or None
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "8"))

# 0 = run forever (VM / Docker). Any positive value = exit after N seconds,
# which is what the GitHub Actions workflow uses.
RUN_SECONDS = float(os.getenv("RUN_SECONDS", "0"))

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# Any Nike thread whose title matches one of these gets tracked.
# Keep these loose. Nike titles them "Nike Kobe 5 Protro 'Dodgers'" etc.
KEYWORDS = [
    "kobe 5",
    "kobe v ",
    "kobe 6",
    "kobe vi ",
    "kobe protro",
]

# Exact style codes get their own dedicated high-frequency lookup.
# Add codes as they leak. Format: XXXXXX-NNN
STYLE_CODES = [
    "IO6256-400",   # Kobe 5 Protro "Dodgers"
    "IO6261-001",   # Kobe 6 Protro "Coals"
]

# Nike consumer channels. The SNKRS channel is where shock drops surface first.
CHANNELS = {
    "SNKRS": "010794e5-35fe-4e32-aaff-cd2c74f89d61",
    "Nike.com": "d9a5bc42-4b9c-4976-858a-f159cf99c647",
}

FEED = "https://api.nike.com/product_feed/threads/v2"

FIELDS = (
    "&fields=id"
    "&fields=lastFetchTime"
    "&fields=productInfo"
    "&fields=publishedContent.properties.seo"
    "&fields=publishedContent.properties.publish"
    "&fields=publishedContent.properties.threadType"
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]


def headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nike.com/launch",
    }


# ----------------------------------------------------------------------------
# STATE
# ----------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------------
# NIKE PARSING
# ----------------------------------------------------------------------------

def matches_watchlist(title, style):
    low = (title or "").lower()
    if style in STYLE_CODES:
        return True
    return any(k in low for k in KEYWORDS)


def parse_thread(thread):
    """Flatten one Nike thread into the fields we actually care about."""
    out = []
    props = (thread.get("publishedContent") or {}).get("properties") or {}
    seo = props.get("seo") or {}
    slug = seo.get("slug")
    title = seo.get("title") or seo.get("slug") or "Unknown"

    for p in thread.get("productInfo") or []:
        mp = p.get("merchProduct") or {}
        style = mp.get("styleColor")
        if not style:
            continue

        # skuId -> human size
        size_map = {s.get("id"): s.get("nikeSize") for s in (p.get("skus") or [])}
        in_stock = sorted(
            size_map.get(a.get("id"), "?")
            for a in (p.get("availableSkus") or [])
            if a.get("available")
        )

        price = (p.get("merchPrice") or {}).get("currentPrice")
        launch = (p.get("launchView") or {})

        out.append({
            "thread_id": thread.get("id"),
            "title": title,
            "style": style,
            "status": mp.get("status"),                 # ACTIVE / HOLD / CLOSEOUT
            "start": mp.get("commerceStartDate"),
            "method": launch.get("method"),             # DRAW / LEO / None (FLOW = first come)
            "price": price,
            "sizes": in_stock,
            "url": f"https://www.nike.com/t/{slug}" if slug else "https://www.nike.com/launch",
        })
    return out


def diff_and_alert(prev, cur):
    """Return (alert_title, alert_body, priority) or None."""
    sizes = cur["sizes"]
    label = f"{cur['title']} ({cur['style']})"
    size_line = ", ".join(sizes) if sizes else "none listed yet"
    tail = f"\nSizes: {size_line}"
    if cur.get("price"):
        tail += f"\nPrice: ${cur['price']}"
    if cur.get("method"):
        tail += f"\nMethod: {cur['method']}"

    if prev is None:
        if sizes:
            return ("LIVE NOW", f"{label} just appeared IN STOCK{tail}", 5)
        return ("New listing", f"{label} posted, not live yet{tail}", 4)

    was = set(prev.get("sizes") or [])
    now = set(sizes)

    if not was and now:
        return ("RESTOCK", f"{label} back in stock{tail}", 5)

    added = sorted(now - was)
    if added:
        return ("Sizes added", f"{label} added: {', '.join(added)}{tail}", 5)

    if prev.get("status") != cur.get("status") and cur.get("status") == "ACTIVE":
        return ("Went ACTIVE", f"{label} flipped to ACTIVE{tail}", 5)

    return None


# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------

async def get_json(session, url, tries=3):
    for attempt in range(tries):
        try:
            async with session.get(url, headers=headers(), proxy=PROXY_URL,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
                if r.status in (403, 429):
                    # Backed off, not banned. Sleep and rotate UA on retry.
                    await asyncio.sleep(2 ** attempt + random.random())
                    continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1 + attempt)
    return None


async def poll_channel(session, name, channel_id):
    url = (f"{FEED}?filter=marketplace(US)&filter=language(en)"
           f"&filter=channelId({channel_id})&count=60{FIELDS}")
    data = await get_json(session, url)
    if not data:
        return []
    results = []
    for thread in data.get("objects") or []:
        for item in parse_thread(thread):
            if matches_watchlist(item["title"], item["style"]):
                item["source"] = name
                results.append(item)
    return results


async def poll_style(session, style):
    url = (f"{FEED}?filter=marketplace(US)&filter=language(en)"
           f"&filter=productInfo.merchProduct.styleColor({style}){FIELDS}")
    data = await get_json(session, url)
    if not data:
        return []
    results = []
    for thread in data.get("objects") or []:
        for item in parse_thread(thread):
            item["source"] = "StyleCode"
            results.append(item)
    return results


# ----------------------------------------------------------------------------
# PUSH
# ----------------------------------------------------------------------------

async def push(session, title, body, url, priority=5):
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC] {title}: {body}")
        return
    try:
        await session.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title[:200],
                "Priority": str(priority),
                "Tags": "fire,basketball",
                "Click": url,
                "Actions": f"view, Open, {url}",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        )
    except Exception as e:
        print(f"push failed: {e}")


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------

async def main():
    state = load_state()
    print(f"Monitoring {len(STYLE_CODES)} style codes + {len(CHANNELS)} channels "
          f"every ~{POLL_SECONDS}s. Topic: {NTFY_TOPIC or '(stdout only)'}")

    deadline = time.monotonic() + RUN_SECONDS if RUN_SECONDS else None

    async with aiohttp.ClientSession() as session:
        # Skip the "online" ping in CI mode, otherwise you get one every 5 min.
        if NTFY_TOPIC and not RUN_SECONDS:
            await push(session, "Monitor online",
                       "Kobe 5/6 monitor started and is watching.",
                       "https://www.nike.com/launch", priority=3)

        while True:
            if deadline and time.monotonic() >= deadline:
                print("run window finished")
                return
            started = time.monotonic()
            tasks = [poll_channel(session, n, c) for n, c in CHANNELS.items()]
            tasks += [poll_style(session, s) for s in STYLE_CODES]

            try:
                batches = await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                print(f"poll error: {e}")
                batches = []

            seen_now = {}
            for batch in batches:
                if isinstance(batch, Exception) or not batch:
                    continue
                for item in batch:
                    key = f"{item['style']}"
                    # Prefer the record with the most stock info
                    if key not in seen_now or len(item["sizes"]) > len(seen_now[key]["sizes"]):
                        seen_now[key] = item

            dirty = False
            for key, item in seen_now.items():
                prev = state.get(key)
                alert = diff_and_alert(prev, item)
                if alert:
                    title, body, prio = alert
                    stamp = time.strftime("%H:%M:%S")
                    print(f"[{stamp}] {title} :: {item['title']} :: {item['sizes']}")
                    await push(session, f"{title}: {item['title']}", body,
                               item["url"], prio)
                state[key] = {
                    "status": item["status"],
                    "sizes": item["sizes"],
                    "title": item["title"],
                    "last_seen": time.time(),
                }
                dirty = True

            if dirty:
                save_state(state)

            elapsed = time.monotonic() - started
            # Jitter so requests don't land on a perfectly predictable cadence.
            sleep_for = max(1.0, POLL_SECONDS - elapsed) + random.uniform(0, 1.5)
            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
