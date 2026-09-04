#!/usr/bin/env python3
"""Maintain docs/schedule.json from the One Piece title page on MANGA Plus.

The title page (https://mangaplus.shueisha.co.jp/titles/100020) is a JavaScript
application: its HTML carries no chapter data. The page loads everything from the
endpoint below, which answers with protobuf and requires a client-generated
SESSION-TOKEN header. This script speaks to that same endpoint and decodes the
wire format directly, so it needs no third-party packages.

Failure is never silent. Any network, decode, or sanity-check problem leaves the
previous JSON in place, marks it stale, and exits non-zero.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request

TITLE_ID = 100020
SOURCE_URL = f"https://mangaplus.shueisha.co.jp/titles/{TITLE_ID}"
API_URL = (
    "https://jumpg-webapi.tokyo-cdn.com/api/title_detailV3"
    f"?title_id={TITLE_ID}&clang=eng"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs",
    "schedule.json",
)

# Field numbers in the title_detailV3 response. Named so a structure change
# shows up as a readable error rather than a wrong number.
F_SUCCESS = 1
F_ERROR = 2
F_TITLE_DETAIL = 8
F_TITLE = 1
F_NEXT_TIMESTAMP = 5
F_CHAPTER_GROUP = 28
F_TITLE_ID = 1
F_TITLE_NAME = 2
F_GROUP_CHAPTER_LISTS = (2, 3, 4)  # first / mid / last chapter list
F_CHAPTER_NAME = 3
F_CHAPTER_START = 6

DAY = 86400
MAX_RELEASE_AGE_DAYS = 400
MAX_NEXT_HORIZON_DAYS = 120
BREAK_GAP_DAYS = 7
RECENT_WINDOW_DAYS = 180

# Selector override so the failure path can be exercised on demand. Setting it
# to a field number that is not in the response makes parsing fail the way a
# structure change would. Scheduled runs pass this through empty.
_override = os.environ.get("MANGAPLUS_CHAPTER_GROUP_FIELD", "").strip()
CHAPTER_GROUP_FIELD = int(_override) if _override else F_CHAPTER_GROUP


# What went wrong, in terms the page can turn into a sentence for the reader.
# The raw message stays alongside it for whoever maintains the job.
SOURCE_UNREACHABLE = "source_unreachable"  # no answer: network, timeout, non-200
SOURCE_REFUSED = "source_refused"          # answered, and declined to serve the data
FORMAT_CHANGED = "format_changed"          # answered, in a shape we no longer recognise
DATA_REJECTED = "data_rejected"            # read cleanly, but the values failed a sanity check


class ParseError(Exception):
    """The response did not look like the title page data we know how to read."""

    def __init__(self, message: str, code: str = FORMAT_CHANGED):
        super().__init__(message)
        self.code = code


# --- minimal protobuf wire reader -------------------------------------------


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ParseError("truncated varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ParseError("varint too long")


def parse_message(buf: bytes) -> dict[int, list]:
    """Decode one protobuf message into {field_number: [value, ...]}.

    Varints become ints, length-delimited fields stay bytes. Groups are not
    used by this API and are rejected.
    """
    fields: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ParseError("field number 0")
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire == 2:
            length, i = _read_varint(buf, i)
            value, i = buf[i : i + length], i + length
            if len(value) != length:
                raise ParseError("truncated length-delimited field")
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        else:
            raise ParseError(f"unsupported wire type {wire}")
        fields.setdefault(field, []).append(value)
    return fields


def one(fields: dict[int, list], field: int, what: str):
    values = fields.get(field)
    if not values:
        raise ParseError(f"missing {what} (field {field})")
    return values[0]


def sub(fields: dict[int, list], field: int, what: str) -> dict[int, list]:
    raw = one(fields, field, what)
    if not isinstance(raw, bytes):
        raise ParseError(f"{what} (field {field}) is not a message")
    return parse_message(raw)


# --- MANGA Plus ---------------------------------------------------------------


def session_token() -> str:
    """The web app stores a random hex UUID locally and sends it as its token."""
    h = "%032x" % random.getrandbits(128)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://mangaplus.shueisha.co.jp/",
            "Origin": "https://mangaplus.shueisha.co.jp",
            "SESSION-TOKEN": session_token(),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise ParseError(f"HTTP {response.status} from MANGA Plus", SOURCE_UNREACHABLE)
        return response.read()


def readable_error(error_fields: dict[int, list]) -> str:
    """Pull the English message out of an error response, if there is one."""
    for popup in error_fields.values():
        for raw in popup:
            if not isinstance(raw, bytes):
                continue
            try:
                text = parse_message(raw).get(2, [b""])[0]
            except ParseError:
                continue
            if isinstance(text, bytes) and text:
                return text.decode("utf-8", "replace").splitlines()[0]
    return "unknown error"


def collect_chapters(detail: dict[int, list]) -> list[tuple[int, int]]:
    """Return [(chapter_number, start_timestamp)] from every chapter list group."""
    chapters: list[tuple[int, int]] = []
    for raw_group in detail.get(CHAPTER_GROUP_FIELD, []):
        if not isinstance(raw_group, bytes):
            continue
        group = parse_message(raw_group)
        for list_field in F_GROUP_CHAPTER_LISTS:
            for raw_chapter in group.get(list_field, []):
                if not isinstance(raw_chapter, bytes):
                    continue
                chapter = parse_message(raw_chapter)
                names = chapter.get(F_CHAPTER_NAME, [])
                starts = chapter.get(F_CHAPTER_START, [])
                if not names or not starts:
                    continue
                match = re.fullmatch(r"#(\d+)", names[0].decode("utf-8", "replace").strip())
                if match and isinstance(starts[0], int):
                    chapters.append((int(match.group(1)), starts[0]))
    return chapters


def scrape() -> dict:
    """Fetch and decode the title page data, or raise ParseError."""
    try:
        body = fetch(API_URL)
    except urllib.error.URLError as exc:
        raise ParseError(f"request failed: {exc}", SOURCE_UNREACHABLE) from exc

    root = parse_message(body)
    if F_SUCCESS not in root:
        if F_ERROR in root:
            raw = root[F_ERROR][0]
            raise ParseError(
                f"MANGA Plus refused: {readable_error(parse_message(raw))}", SOURCE_REFUSED
            )
        raise ParseError("response has neither a success nor an error result")

    success = parse_message(root[F_SUCCESS][0])
    detail = sub(success, F_TITLE_DETAIL, "title detail view")
    title = sub(detail, F_TITLE, "title")

    title_id = one(title, F_TITLE_ID, "title id")
    title_name = one(title, F_TITLE_NAME, "title name").decode("utf-8", "replace")
    if title_id != TITLE_ID or "One Piece" not in title_name:
        raise ParseError(f"unexpected title: id={title_id!r} name={title_name!r}")

    chapters = collect_chapters(detail)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    released = [c for c in chapters if c[1] <= now]
    if not released:
        raise ParseError(
            f"no released chapters found in {len(chapters)} parsed entries; "
            "the chapter list structure has probably changed"
        )
    released.sort(key=lambda c: (c[1], c[0]))
    latest_chapter, latest_release = released[-1]

    age_days = (now - latest_release) / DAY
    if age_days > MAX_RELEASE_AGE_DAYS:
        raise ParseError(
            f"latest chapter #{latest_chapter} is {age_days:.0f} days old", DATA_REJECTED
        )

    next_raw = detail.get(F_NEXT_TIMESTAMP, [0])[0]
    next_release = next_raw if isinstance(next_raw, int) else 0
    if next_release:
        if next_release <= latest_release:
            raise ParseError(
                "announced next release is not after the latest release", DATA_REJECTED
            )
        if (next_release - now) / DAY > MAX_NEXT_HORIZON_DAYS:
            raise ParseError(
                f"announced next release is {(next_release - now) / DAY:.0f} days out",
                DATA_REJECTED,
            )

    # Keep the recent release timestamps so the page can work out where we are in
    # the three-on / one-off rotation when MANGA Plus has not announced a date.
    # The response also carries the 2019 launch chapters; those say nothing about
    # the current rotation, so only the last few months count.
    cutoff = latest_release - RECENT_WINDOW_DAYS * DAY
    recent = sorted({c[1] for c in released if c[1] >= cutoff})[-6:]

    return {
        "latest_chapter": latest_chapter,
        "latest_release_utc": iso(latest_release),
        "next_chapter": latest_chapter + 1,
        "next_release_utc": iso(next_release) if next_release else None,
        "next_confirmed": bool(next_release),
        "recent_releases_utc": [iso(ts) for ts in recent],
        "source_url": SOURCE_URL,
    }


def iso(timestamp: int) -> str:
    return (
        dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- file handling ------------------------------------------------------------


def load_previous() -> dict | None:
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def content_of(payload: dict) -> dict:
    """Everything except the timestamp, so a re-check is not a change."""
    return {k: v for k, v in payload.items() if k != "updated_at_utc"}


def write(payload: dict) -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main() -> int:
    previous = load_previous()
    now_iso = iso(int(dt.datetime.now(dt.timezone.utc).timestamp()))

    try:
        scraped = scrape()
    except (ParseError, OSError) as exc:
        reason = str(exc)
        code = exc.code if isinstance(exc, ParseError) else SOURCE_UNREACHABLE
        print(f"update failed [{code}]: {reason}", file=sys.stderr)
        if previous is None:
            print("no previous schedule.json to preserve", file=sys.stderr)
            return 1
        # Keep the old data untouched, including updated_at_utc: it stays the age
        # of the data, which is exactly what the stale notice needs to show.
        stale = dict(previous)
        stale["stale"] = True
        stale["stale_reason_code"] = code
        stale["stale_reason"] = reason
        stale["stale_since_utc"] = previous.get("stale_since_utc") or now_iso
        if content_of(stale) != content_of(previous):
            write(stale)
            print("marked schedule.json stale", file=sys.stderr)
        else:
            print("schedule.json already stale for the same reason", file=sys.stderr)
        return 1

    payload = dict(scraped)
    payload["stale"] = False
    payload["updated_at_utc"] = now_iso

    if previous is not None and content_of(payload) == content_of(previous):
        print("no change")
        return 0

    write(payload)
    print(f"schedule: {payload['latest_chapter']} -> {payload['next_chapter']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
