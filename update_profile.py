#!/usr/bin/env python3
"""Refresh the profile from public GitHub data, using only Python's standard library.

Every invocation fetches GitHub again. A failed or inconsistent fetch exits before
writing any files, preserving the last valid graph and text. No token is read.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


START_MARKER = "<!-- profile-stats:start -->"
END_MARKER = "<!-- profile-stats:end -->"
ASSET = "hollow-knight-contributions-live.svg"
DATA = "hollow-knight-contributions-live.json"
PALETTE = ("#142b3e", "#31576b", "#527f92", "#86b6c5", "#d2eef2")


class CalendarParser(HTMLParser):
    """Extract GitHub's date levels, exact tooltip counts and displayed total."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = {}
        self.tips = {}
        self.tip_id = None
        self.tip_text = []
        self.heading = []
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td" and "data-date" in attrs:
            cell_id = attrs.get("id")
            day = attrs["data-date"]
            if not cell_id or cell_id in self.cells:
                raise ValueError("Missing or duplicate GitHub calendar cell id")
            self.cells[cell_id] = {"date": day, "level": int(attrs["data-level"])}
        if tag == "tool-tip":
            self.tip_id = attrs.get("for")
            self.tip_text = []
        if tag == "h2" and attrs.get("id") == "js-contribution-activity-description":
            self.in_heading = True

    def handle_data(self, text):
        if self.tip_id:
            self.tip_text.append(text)
        if self.in_heading:
            self.heading.append(text)

    def handle_endtag(self, tag):
        if tag == "tool-tip":
            if self.tip_id:
                self.tips[self.tip_id] = " ".join("".join(self.tip_text).split())
            self.tip_id = None
        if tag == "h2":
            self.in_heading = False


def download(url, content_type):
    """Retry transient failures; request English and no cached calendar copy."""
    headers = {
        "User-Agent": "nhphuc09-profile-refresh/1.0",
        "Accept": content_type,
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                if response.status != 200:
                    raise ValueError(f"GitHub returned HTTP {response.status}")
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise ValueError("Unexpectedly large GitHub response")
                return raw
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable fetch state")


def fetch_calendar(username, now):
    url = f"https://github.com/users/{username}/contributions"
    raw = download(url, "text/html")
    parser = CalendarParser()
    parser.feed(raw.decode("utf-8"))
    heading = " ".join("".join(parser.heading).split())
    total_match = re.search(r"([\d,]+)\s+contributions?\b", heading)
    if not total_match:
        raise ValueError("Cannot find GitHub's contribution total; keeping previous data")
    total = int(total_match.group(1).replace(",", ""))
    days = []
    for cell_id, item in parser.cells.items():
        tooltip = parser.tips.get(cell_id, "")
        count_match = re.fullmatch(r"(No|[\d,]+) contributions? on .+", tooltip)
        if not count_match:
            raise ValueError(f"Missing exact contribution count for {item['date']}")
        count = 0 if count_match[1] == "No" else int(count_match[1].replace(",", ""))
        if not 0 <= item["level"] <= 4 or ((count == 0) != (item["level"] == 0)):
            raise ValueError(f"Inconsistent calendar level for {item['date']}")
        date.fromisoformat(item["date"])
        days.append({**item, "count": count})
    days.sort(key=lambda item: item["date"])
    if not 350 <= len(days) <= 378 or len({item["date"] for item in days}) != len(days):
        raise ValueError("GitHub did not return a complete, unique yearly calendar")
    first, last = (date.fromisoformat(days[index]["date"]) for index in (0, -1))
    if (last - first).days + 1 != len(days) or first.weekday() != 6:
        raise ValueError("GitHub calendar dates are discontinuous or not Sunday aligned")
    if not 0 <= (now.date() - last).days <= 2:
        raise ValueError("GitHub returned a stale or future calendar; keeping previous data")
    if sum(item["count"] for item in days) != total:
        raise ValueError("Day counts do not match GitHub's displayed total; keeping previous data")
    return {
        "username": username,
        "source_url": url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_heading": heading,
        "total_contributions": total,
        "date_range": {"start": str(first), "end": str(last)},
        "days": days,
    }


def fetch_profile(username):
    """Public profile only: no email, credentials, private repository or secret access."""
    url = f"https://api.github.com/users/{username}"
    profile = json.loads(download(url, "application/vnd.github+json"))
    return {
        "source_url": url,
        "public_repositories": int(profile["public_repos"]),
        "followers": int(profile["followers"]),
    }


def ornament(x, y, reflected=False):
    scale = -1 if reflected else 1
    return f'''<g transform="translate({x} {y}) scale({scale} 1)" fill="none" stroke="#8db8c8" stroke-opacity=".55" stroke-width="1.6"><path d="M0 0C-28-1-34-19-56-19C-81-19-83 4-67 5C-56 6-51-5-59-9C-67-13-74-5-68-2M-8 0C-29 4-31 23-52 21C-72 19-75 6-64 3M-18-4C-33-16-39-33-56-34C-70-35-74-23-65-20"/></g>'''


def render_svg(data):
    """Native SVG text and shapes stay sharp at 2560 px and at higher densities."""
    username = escape(data["username"])
    total = data["total_contributions"]
    first = date.fromisoformat(data["date_range"]["start"])
    last = date.fromisoformat(data["date_range"]["end"])
    days = data["days"]
    columns = (len(days) + 6) // 7
    step, cell = 40, 30
    grid_width = (columns - 1) * step + cell
    gx = (2560 - grid_width) // 2 + 12
    gy = 244
    updated = data["updated_at_utc"].replace("T", " ").replace("+00:00", " UTC")
    pieces = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="2560" height="720" viewBox="0 0 2560 720" role="img" aria-labelledby="title description">
<title id="title">{total:,} contributions in the last year — {username}</title>
<desc id="description">Actual GitHub calendar levels and exact daily counts. Refreshed every six hours by GitHub Actions, last successful update {escape(updated)}. Each day contains its actual count in a title. The native interactive calendar is on the GitHub profile.</desc>
<defs>
  <linearGradient id="night" x2="0" y2="1"><stop stop-color="#081321"/><stop offset="1" stop-color="#10273b"/></linearGradient>
  <radialGradient id="mist"><stop stop-color="#76bdcc" stop-opacity=".11"/><stop offset="1" stop-color="#76bdcc" stop-opacity="0"/></radialGradient>
</defs>
<rect width="2560" height="720" fill="url(#night)"/>
<ellipse cx="1280" cy="650" rx="1270" ry="550" fill="url(#mist)"/>
<g fill="none" stroke="#557e94" stroke-opacity=".22" stroke-width="2"><path d="M50 720V175Q50 55 160 18Q270 55 270 175V720M2290 720V175Q2290 55 2400 18Q2510 55 2510 175V720"/><path d="M82 720V184Q82 90 160 54Q238 90 238 184V720M2322 720V184Q2322 90 2400 54Q2478 90 2478 184V720"/></g>
{ornament(740, 94)}{ornament(1820, 94, True)}
<g font-family="Georgia,Times New Roman,serif" text-anchor="middle">
  <text x="1280" y="105" fill="#e0f2f4" font-size="54" letter-spacing="2">{total:,} contributions in the last year</text>
  <text x="1280" y="158" fill="#91b9c9" font-family="Arial,Helvetica,sans-serif" font-size="26" letter-spacing="3">@{username} · GITHUB ACTIVITY</text>
</g>''']
    seen_months = set()
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    if (next_month - first).days // 7 < 3:
        seen_months.add(first.strftime("%Y-%m"))
    for index, item in enumerate(days):
        day = date.fromisoformat(item["date"])
        col, row = divmod(index, 7)
        x, y = gx + col * step, gy + row * step
        month = day.strftime("%Y-%m")
        if month not in seen_months:
            seen_months.add(month)
            pieces.append(f'<text x="{x}" y="213" fill="#abcbd5" font-family="Arial,Helvetica,sans-serif" font-size="26">{day:%b}</text>')
        count, level = item["count"], item["level"]
        label = f"{count:,} contribution{'s' if count != 1 else ''} on {day:%d %b %Y}"
        pieces.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="5" fill="{PALETTE[level]}" stroke="#a3cbda" stroke-opacity="{0.55 if level else 0.2}" stroke-width="1.4"><title>{label}</title></rect>')
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        pieces.append(f'<text x="{gx-24}" y="{gy+row*step+23}" fill="#91b1c0" text-anchor="end" font-family="Arial,Helvetica,sans-serif" font-size="24">{label}</text>')
    pieces += [f'<text x="{gx}" y="576" fill="#9ebdcc" font-family="Arial,Helvetica,sans-serif" font-size="25">{first:%d %b %Y} — {last:%d %b %Y}</text>', f'<text x="{gx+grid_width-375}" y="576" fill="#9ebdcc" font-family="Arial,Helvetica,sans-serif" font-size="24">Less</text>']
    for index, color in enumerate(PALETTE):
        pieces.append(f'<rect x="{gx+grid_width-303+index*42}" y="551" width="30" height="30" rx="5" fill="{color}" stroke="#a3cbda" stroke-opacity=".25" stroke-width="1.4"/>')
    pieces += [f'<text x="{gx+grid_width}" y="576" text-anchor="end" fill="#9ebdcc" font-family="Arial,Helvetica,sans-serif" font-size="24">More</text>', '<path d="M180 627H2380" stroke="#709bad" stroke-opacity=".3" stroke-width="1.5"/>', f'<text x="1280" y="678" text-anchor="middle" fill="#8cabbc" font-family="Arial,Helvetica,sans-serif" font-size="23">PUBLIC GITHUB DATA · UPDATED {escape(updated)} · REFRESH EVERY 6 HOURS</text>', '</svg>']
    # Decorative motion stays outside data cells; it never changes counts or levels.
    motes = []
    for index, (x, y) in enumerate(((105, 110), (340, 173), (585, 60), (2060, 65), (2230, 170), (2450, 320), (109, 516), (560, 610), (1960, 609))):
        duration = 5 + index % 4
        motes.append(f'''<g opacity=".5"><circle cx="{x}" cy="{y}" r="8" fill="#88d8e7" opacity=".10"/><circle cx="{x}" cy="{y}" r="2.3" fill="#d4f8fb"><animate attributeName="opacity" values=".15;.85;.15" dur="{duration}s" begin="-{index}s" repeatCount="indefinite"/><animate attributeName="cy" values="{y};{y-12};{y}" dur="{duration+2}s" begin="-{index}s" repeatCount="indefinite"/></circle></g>''')
    pieces[-1:-1] = motes
    result = "\n".join(pieces) + "\n"
    ET.fromstring(result)
    return result


def update_readme(content, data):
    start_count, end_count = content.count(START_MARKER), content.count(END_MARKER)
    if start_count == 0 and end_count == 0:
        return content
    if start_count != 1 or end_count != 1 or content.index(START_MARKER) > content.index(END_MARKER):
        raise ValueError("Invalid README profile-stats markers; keeping previous data")
    timestamp = datetime.fromisoformat(data["updated_at_utc"]).strftime("%d %b %Y, %H:%M UTC")
    stats = f'''{START_MARKER}
**{data['total_contributions']:,} contributions in the last year**

<sub>Updated {timestamp} · Refreshes every 6 hours from [GitHub]({data['source_url']}).</sub>
{END_MARKER}'''
    before, rest = content.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    return before + stats + after


def version_readme_images(content, data):
    """Give only the generated live chart img URLs a UTC cache version."""
    updated = datetime.fromisoformat(data["updated_at_utc"]).astimezone(timezone.utc)
    version = updated.strftime("%Y%m%d%H%M%S")
    # Consume quoted attribute values as whole tokens, so an example URL inside
    # alt text cannot be mistaken for the actual image source attribute.
    image_tag = re.compile(r'''<img\b(?:[^>"']|"[^"]*"|'[^']*')*>''', re.IGNORECASE)
    attribute = re.compile(
        r'''(?P<space>\s+)(?P<name>[^\s=/>]+)(?:(?P<equals>\s*=\s*)(?P<value>"[^"]*"|'[^']*'|[^\s>]+))?'''
    )
    target = re.compile(r"(?P<path>\./(?:hollow-knight-contributions-live|assets/contributions-live)\.svg)(?:\?.*)?")

    def replace_attribute(match):
        value = match["value"] or ""
        if match["name"].lower() != "src" or len(value) < 2 or value[0] not in "\"'":
            return match.group(0)
        source = target.fullmatch(value[1:-1])
        if not source:
            return match.group(0)
        return f"{match['space']}{match['name']}{match['equals']}{value[0]}{source['path']}?v={version}{value[-1]}"

    def replace_tag(match):
        return attribute.sub(replace_attribute, match.group(0))

    return image_tag.sub(replace_tag, content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="nhphuc09")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.username):
        parser.error("Invalid GitHub username")
    root = args.root.resolve()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = fetch_calendar(args.username, now)
    data["profile"] = fetch_profile(args.username)
    data["updated_at_utc"] = now.isoformat()
    svg = render_svg(data)
    readme_path = root / "README.md"
    original_readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else None
    readme = (
        version_readme_images(update_readme(original_readme, data), data)
        if original_readme is not None else None
    )
    root.mkdir(parents=True, exist_ok=True)
    # Finish all network, parsing and validation before replacing published files.
    (root / ASSET).write_text(svg, encoding="utf-8")
    (root / DATA).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if readme is not None and readme != original_readme:
        readme_path.write_text(readme, encoding="utf-8")
    print(json.dumps({
        "source": data["source_url"],
        "actual_total": data["total_contributions"],
        "sum_of_daily_counts": sum(day["count"] for day in data["days"]),
        "date_range": data["date_range"],
        "day_count": len(data["days"]),
        "updated_at_utc": data["updated_at_utc"],
        "svg": str(root / ASSET),
        "readme_stats_updated": readme != original_readme,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, HTTPError, URLError, TimeoutError) as error:
        print(f"Profile refresh failed: {error}", file=sys.stderr)
        sys.exit(1)
