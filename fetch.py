#!/usr/bin/env python3
"""Turn Polymarket AI markets into data.json.

Two shapes, both read straight off Polymarket's own tags rather than off market
names, so a renamed bet keeps working:

  ai-releases  ->  "released by DATE?" ladders. Each is a CDF: P(shipped by
                   date). Cross at 0.5 -> median expected release date; at
                   0.25/0.75 -> an uncertainty band.
  ai-rankings  ->  "who has the best model" leaderboard. One market per lab, so
                   it covers the labs that have no release market at all
                   (Mistral, Meta, DeepSeek, Thinking Machines).

stdlib only, so the GitHub Action needs no dependency step.
Run `python3 fetch.py --test` for the self-check.
"""

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com/events"
# Polymarket curates these tags. Trusting its taxonomy is what keeps us out of
# the business of pattern-matching bet titles: a ladder renamed from "GPT-6
# released by…?" to anything else still carries the tag, and unrelated date
# ladders in the broader "ai" tag ("Trump orders federal review of AI model
# releases by...?", "AI data center in space by...?") never reach us.
LADDER_TAG = "ai-releases"
RANKING_TAG = "ai-rankings"
PAGE = 100  # gamma caps a page here, and silently truncates rather than erroring

# Tunables — every one of these earns its place against the live feed.
MIN_MARKET_VOLUME = 50  # below this a price is noise. Thin ladders (Grok 5, MAI)
# trade in the hundreds, so a $1000 floor deleted five labs; confidence carries
# the warning instead.
MIN_RUNGS = 2  # a CDF needs two points. Also what drops score ladders, whose
# rungs all share one date ("...at least 1470 by December 31, 2026?").
MIN_ENTRIES = 8  # a leaderboard worth showing
RANKING_MIN_DAYS = 7  # a board resolving sooner than this is about to go stale
HIGH_VOLUME = 100_000
MED_VOLUME = 10_000

MONTHS = {
    m: i
    for i, m in enumerate(
        "january february march april may june july august september "
        "october november december".split(),
        1,
    )
}
# Year is REQUIRED. "released by December 31?" (no year) is last year's resolved
# market; inferring a year silently resurrects it.
DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I
)
# "released BY <date>" only, and the preposition does real work: the sibling
# "released ON <date>" event prices each single day (a PDF), and reading those as
# cumulative gives a confidently wrong median.
BY_RE = re.compile(r"\bby\b", re.I)
ON_RE = re.compile(r"\breleased\s+on\b", re.I)
LADDER_RE = re.compile(r"releas\w*\s+by\b", re.I)
# "Will <entity> have the best..." / "Will <entity> be the first..."
ENTITY_RE = re.compile(r"^will\s+(.+?)\s+(?:have|be|hit|reach)\b", re.I)
# Unallocated leaderboard slots Polymarket ships pre-created at p=0.5, vol=0.
PLACEHOLDER_RE = re.compile(r"^(company [a-z]|any other|no company)\b", re.I)

# Tag label / title keyword -> display name. Only an alias table: anything
# unmatched falls through to the entity's own name rather than disappearing.
LABS = [
    ("OpenAI", ("openai", "gpt", "chatgpt", "sam altman")),
    ("Google", ("google", "gemini")),
    ("Anthropic", ("anthropic", "claude", "mythos", "haiku", "sonnet", "opus", "fable")),
    ("xAI", ("xai", "spacexai", "grok", "elon musk")),
    ("Meta", ("meta", "llama")),
    ("Microsoft", ("microsoft", "mai")),
    ("DeepSeek", ("deepseek",)),
    ("Alibaba", ("qwen", "alibaba")),
    ("Moonshot", ("moonshot", "kimi")),
    ("Mistral", ("mistral",)),
    ("Thinking Machines", ("thinky", "thinking machines")),
]


def pull(tag):
    """Every open event on a tag. Paginated — a bare limit=100 silently drops the
    tail, which is where the thin ladders (Grok 5, Kimi K4, Claude Sonnet) live."""
    out, offset = [], 0
    while True:
        url = "%s?tag_slug=%s&closed=false&active=true&limit=%d&offset=%d" % (
            GAMMA,
            tag,
            PAGE,
            offset,
        )
        # Gamma 403s urllib's default User-Agent; any real-looking one is fine.
        req = urllib.request.Request(url, headers={"User-Agent": "whats-the-next-model/1.0"})
        with urllib.request.urlopen(req, timeout=60) as response:
            page = json.load(response)
        out += page
        if len(page) < PAGE:
            return out
        offset += PAGE


def rung_date(question):
    """Date this rung resolves on, parsed out of the question text.

    Never from the market's own endDate — that field is wrong on ladder markets
    in the live feed (a "September 30, 2026" question ships endDate 2026-06-30).
    """
    if ON_RE.search(question):
        return None  # per-day probability, not a cumulative rung
    by = BY_RE.search(question)
    if not by:
        return None
    found = DATE_RE.search(question, by.end())
    if not found:
        return None
    month, day, year = found.groups()
    try:
        return date(int(year), MONTHS[month.lower()], int(day))
    except ValueError:  # e.g. February 30
        return None


def yes_price(market):
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        prices = json.loads(prices)
    if not prices:
        return None
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    idx = 0
    if outcomes and "yes" in [str(o).lower() for o in outcomes]:
        idx = [str(o).lower() for o in outcomes].index("yes")
    try:
        return float(prices[idx])
    except (ValueError, IndexError, TypeError):
        return None


def live(market, floor=MIN_MARKET_VOLUME):
    """Price of a market still worth reading, else None.

    closed=false filters *events*; roughly half the markets inside an open event
    are already settled, so each one needs checking on its own.
    """
    if market.get("closed"):
        return None
    p = yes_price(market)
    if p is None or p <= 0 or p >= 1:
        return None  # 0 and 1 mean resolved, not impossible/certain
    if float(market.get("volume") or 0) < floor:
        return None
    return p


def rungs_of(event, today):
    """Usable (date, p, volume) ladder rungs, soonest first, deduped by date."""
    by_date = {}
    for market in event.get("markets") or []:
        when = rung_date(market.get("question") or "")
        if when is None or when <= today:
            continue  # unparseable, or already in the past
        p = live(market)
        if p is None:
            continue
        volume = float(market.get("volume") or 0)
        if when not in by_date or volume > by_date[when][1]:
            by_date[when] = (p, volume)
    return [
        {"date": d, "p": by_date[d][0], "v": by_date[d][1]} for d in sorted(by_date)
    ]


def to_cdf(rungs, today):
    """Clamp to non-decreasing and anchor at zero today.

    The raw feed is not monotonic (a later date can be priced below an earlier
    one); a CDF must be. Running max is the fix, and it keeps the more-liquid
    early rungs rather than letting a thin late one drag the curve down.
    """
    curve = [{"date": today, "p": 0.0, "v": 0.0}]
    running = 0.0
    for rung in rungs:
        running = max(running, rung["p"])
        curve.append({"date": rung["date"], "p": running, "v": rung["v"]})
    return curve


def crossing(curve, q):
    """Date the curve crosses probability q, linearly interpolated. None if never."""
    prev = None
    for point in curve:
        if point["p"] >= q:
            if prev is None:
                return point["date"]
            span_days = (point["date"] - prev["date"]).days
            rise = point["p"] - prev["p"]
            if rise <= 0 or span_days <= 0:
                return point["date"]
            offset = (q - prev["p"]) / rise * span_days
            return prev["date"] + timedelta(days=round(offset))
        prev = point
    return None


def name_to_lab(text):
    lowered = text.lower()
    for name, keys in LABS:
        if any(re.search(r"\b%s\b" % re.escape(k), lowered) for k in keys):
            return name
    return None


def lab_of(event):
    for tag in event.get("tags") or []:
        hit = name_to_lab(str(tag.get("label", "")))
        if hit:
            return hit
    return name_to_lab(event.get("title") or "") or "Other"


def model_name(title):
    """The model, from the event title minus its ladder phrasing.

    A retitled event keeps its (uglier) full title rather than vanishing.
    """
    name = LADDER_RE.split(title)[0].strip()
    name = re.sub(r"[\s….:\-–?]+$", "", name)
    return name or title.strip()


def confidence(volume, n_rungs):
    if volume > HIGH_VOLUME and n_rungs >= 4:
        return "high"
    if volume > MED_VOLUME:
        return "medium"
    return "low"


def iso(d):
    return d.isoformat() if d else None


def ladders(events, today):
    """Every event on the tag that is shaped like a date ladder.

    No title matching: shape decides. Score ladders collapse to one rung (all
    their questions share a date) and single-question events never reach two, so
    both fall out on MIN_RUNGS.
    """
    models = []
    for event in events:
        rungs = rungs_of(event, today)
        if len(rungs) < MIN_RUNGS:
            continue
        curve = to_cdf(rungs, today)
        median = crossing(curve, 0.5)
        volume = float(event.get("volume") or 0)
        models.append(
            {
                "name": model_name(event.get("title") or ""),
                "lab": lab_of(event),
                "median_date": iso(median),
                # No median means the market never gets to 50% within its own
                # horizon — say "later than", never invent a date.
                "not_before": None if median else iso(curve[-1]["date"]),
                "p25": iso(crossing(curve, 0.25)),
                "p75": iso(crossing(curve, 0.75)),
                "curve": [
                    {"date": c["date"].isoformat(), "p": round(c["p"], 4)}
                    for c in curve
                ],
                "confidence": confidence(volume, len(rungs)),
                "volume_usd": round(volume),
                "source": "https://polymarket.com/event/%s" % event.get("slug", ""),
            }
        )
    # Soonest first; the no-median ones sort to the bottom.
    models.sort(key=lambda m: m["median_date"] or "9999")
    return models


def entries_of(event):
    """(entity, p, volume) per leaderboard market, biggest first."""
    out = []
    for market in event.get("markets") or []:
        found = ENTITY_RE.match(market.get("question") or "")
        if not found:
            continue
        who = found.group(1).strip()
        if PLACEHOLDER_RE.match(who):
            continue  # unallocated slot Polymarket ships at p=0.5, vol=0
        p = live(market)
        if p is None:
            continue
        out.append(
            {
                "name": who,
                "lab": name_to_lab(who) or who,
                "p": round(p, 4),
                "volume_usd": round(float(market.get("volume") or 0)),
            }
        )
    out.sort(key=lambda e: -e["p"])
    return out


def leaderboard(events, today):
    """The busiest ranking board that is still far enough out to be forward-looking.

    endDate is trustworthy here (unlike on ladder rungs, where it is wrong) — it
    is the only date a "best model at the end of August" question carries.
    """
    best = None
    for event in events:
        stamp = (event.get("endDate") or "")[:10]
        try:
            resolves = date.fromisoformat(stamp)
        except ValueError:
            continue
        if (resolves - today).days < RANKING_MIN_DAYS:
            continue  # about to resolve; would show a settled board as a forecast
        entries = entries_of(event)
        if len(entries) < MIN_ENTRIES:
            continue
        volume = float(event.get("volume") or 0)
        if best and volume <= best["volume_usd"]:
            continue
        best = {
            "title": (event.get("title") or "").strip(),
            "resolves": resolves.isoformat(),
            "entries": entries,
            "volume_usd": round(volume),
            "source": "https://polymarket.com/event/%s" % event.get("slug", ""),
        }
    return best


def build(ladder_events, ranking_events, today):
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": today.isoformat(),
        "models": ladders(ladder_events, today),
        "leaderboard": leaderboard(ranking_events, today),
    }


STATIC_START = "<!-- static:start"
STATIC_END = "<!-- static:end -->"


def long_date(stamp):
    """2026-08-21 -> 21 August 2026. strftime's %-d is not portable."""
    d = date.fromisoformat(stamp)
    return "%d %s %d" % (d.day, d.strftime("%B"), d.year)


def static_html(data):
    """The numbers as plain HTML, for whoever never runs the JavaScript.

    index.html draws everything client-side from data.json, so the served
    markup contains no lab, no model and no date — nothing a search engine can
    index or a no-JS reader can read. This block is generated on every run and
    spliced between the static: markers, and the page removes it as soon as the
    real render starts.
    """
    esc = html.escape
    out = ["<section id=\"static\">", "  <h2>Expected release dates</h2>", "  <ul>"]
    for m in data["models"]:
        if m["median_date"]:
            when = "expected <strong>%s</strong>" % long_date(m["median_date"])
            if m["p25"] and m["p75"]:
                when += ", likely between %s and %s" % (long_date(m["p25"]), long_date(m["p75"]))
        else:
            # No 50% crossing means no date: say so rather than extrapolate.
            when = "not before <strong>%s</strong> on current prices" % long_date(m["not_before"])
        if m["confidence"] == "low":
            when += " (thin market)"
        out.append("    <li>%s — %s: %s</li>" % (esc(m["lab"]), esc(m["name"]), when))
    out.append("  </ul>")

    board = data["leaderboard"]
    if board:
        ranked = ", ".join(
            "%s %d%%" % (esc(e["lab"]), round(e["p"] * 100)) for e in board["entries"]
        )
        out += [
            "  <h2>Who&rsquo;s ahead right now</h2>",
            "  <p class=\"sub\">Polymarket&rsquo;s &ldquo;%s&rdquo; board, settling %s: %s.</p>"
            % (esc(board["title"]), long_date(board["resolves"]), ranked),
        ]
    out.append(
        "  <p class=\"sub\">Prediction-market prices as of %s, not release announcements.</p>"
        % long_date(data["as_of"])
    )
    out.append("</section>")
    return "\n".join(out)


def splice_static(data, path="index.html"):
    with open(path) as f:
        page = f.read()
    # Both markers must appear exactly once. A second copy anywhere — a CSS
    # comment mentioning the marker was the real case — makes partition() cut at
    # the wrong place and take the rest of the file with it.
    for marker in (STATIC_START, STATIC_END):
        found = page.count(marker)
        if found != 1:
            raise SystemExit(
                "%s: found %d copies of %r, need exactly 1 — refusing to splice"
                % (path, found, marker)
            )
    head, _, rest = page.partition(STATIC_START)
    _, _, tail = rest.partition(STATIC_END)
    with open(path, "w") as f:
        f.write(
            "%s%s — rewritten by fetch.py on every run. Do not hand-edit. -->\n%s\n%s%s"
            % (head, STATIC_START, static_html(data), STATIC_END, tail)
        )


def main():
    data = build(pull(LADDER_TAG), pull(RANKING_TAG), datetime.now(timezone.utc).date())
    with open("data.json", "w") as out:
        json.dump(data, out, indent=1)
        out.write("\n")
    splice_static(data)
    for m in data["models"]:
        print(
            "%-32s %-18s %s  %-6s $%s"
            % (
                m["name"][:32],
                m["lab"],
                m["median_date"] or "> " + m["not_before"],
                m["confidence"],
                format(m["volume_usd"], ","),
            )
        )
    board = data["leaderboard"]
    if board:
        top = ", ".join("%s %d%%" % (e["lab"], round(e["p"] * 100)) for e in board["entries"][:4])
        print("\n%s (%d labs): %s" % (board["title"], len(board["entries"]), top))
    if not data["models"]:
        print("no usable ladders — check the feed before committing", file=sys.stderr)
        return 1
    return 0


# --- self-check -------------------------------------------------------------
# Fixtures are trimmed real API output (2026-07-28), keeping every gotcha that
# actually bit: a wrong endDate, a no-year resolved market, 0/1 prices, a
# non-monotonic pair, a closed-flag-only market, a "released on" per-day sibling,
# a score ladder, and a leaderboard full of unallocated placeholder slots.
LADDER_FIXTURE = [
    {
        "title": "GPT-6 released by…?",
        "slug": "gpt-6-released-by",
        "volume": 751360,
        "tags": [{"label": "OpenAI"}, {"label": "AI Releases"}],
        "markets": [
            # no year -> last December's resolved market. Must not come back.
            {"question": "Will GPT-6 be released by December 31?",
             "outcomePrices": '["0", "1"]', "volume": 107893, "closed": True},
            # closed flag set but price still looks live: the flag has to be read
            {"question": "Will GPT-6 be released by June 30, 2026?",
             "outcomePrices": '["0.44", "0.56"]', "volume": 220238, "closed": True},
            {"question": "Will GPT-6 be released by July 31, 2026? ",
             "outcomePrices": '["0.0075", "0.9925"]', "volume": 73353},
            {"question": "Will GPT-6 be released by August 7, 2026?",
             "outcomePrices": '["0.017", "0.983"]', "volume": 24989},
            {"question": "Will GPT-6 be released by August 14, 2026?",
             "outcomePrices": '["0.091", "0.909"]', "volume": 37950},
            {"question": "Will GPT-6 be released by August 21, 2026?",
             "outcomePrices": '["0.145", "0.855"]', "volume": 36291},
            {"question": "Will GPT-6 be released by August 31, 2026?",
             "outcomePrices": '["0.26", "0.74"]', "volume": 72312},
            # endDate says June 30 — the question says September 30. Question wins.
            {"question": "Will GPT-6 be released by September 30, 2026?",
             "outcomePrices": '["0.7", "0.3"]', "volume": 94603,
             "endDate": "2026-06-30T00:00:00Z"},
            {"question": "Will GPT-6 be released by December 31, 2026?",
             "outcomePrices": '["0.885", "0.115"]', "volume": 83730},
        ],
    },
    {
        "title": "Next Mythos-Class Model released by…?",
        "slug": "next-mythos-class-model-released-by",
        "volume": 78394,
        "tags": [{"label": "Anthropic"}, {"label": "Claude"}],
        "markets": [
            {"question": "Next Mythos-Class Model released by July 31, 2026?",
             "outcomePrices": '["0.0115", "0.9885"]', "volume": 39030},
            # non-monotonic against September below; the clamp has to fix it
            {"question": "Next Mythos-Class Model released by August 31, 2026?",
             "outcomePrices": '["0.505", "0.495"]', "volume": 16607},
            {"question": "Next Mythos-Class Model released by September 30, 2026?",
             "outcomePrices": '["0.77", "0.23"]', "volume": 22757},
        ],
    },
    {
        # Thin but real: $96 and $216 rungs. A $1000 floor deleted this whole lab.
        "title": "Next Claude Sonnet released by...?",
        "slug": "next-claude-sonnet-released-by",
        "volume": 2966,
        "tags": [{"label": "Sonnet"}, {"label": "Anthropic"}],
        "markets": [
            {"question": "Will the next Claude Sonnet model be released by August 31, 2026?",
             "outcomePrices": '["0.3", "0.7"]', "volume": 216},
            {"question": "Will the next Claude Sonnet model be released by October 31, 2026?",
             "outcomePrices": '["0.76", "0.24"]', "volume": 96},
            {"question": "Will the next Claude Sonnet model be released by December 31, 2026?",
             "outcomePrices": '["0.945", "0.055"]', "volume": 2654},
        ],
    },
    {
        "title": "Grok 5 released by...?",
        "slug": "grok-5-released-by",
        "volume": 2831,
        "tags": [{"label": "Grok"}, {"label": "Elon Musk"}],
        "markets": [
            {"question": "Will Grok 5 be released by August 15, 2026?",
             "outcomePrices": '["0.024", "0.976"]', "volume": 1109},
            {"question": "Will Grok 5 be released by August 31, 2026?",
             "outcomePrices": '["0.037", "0.963"]', "volume": 1175},
            {"question": "Will Grok 5 be released by December 31, 2026?",
             "outcomePrices": '["0.785", "0.215"]', "volume": 547},
        ],
    },
    {
        # Dead event: one live rung left, the rest settled. Not a curve.
        "title": "New Gemini reasoning flagship released by...?",
        "slug": "new-gemini-reasoning-flagship-released-by",
        "volume": 323485,
        "tags": [{"label": "google"}],
        "markets": [
            {"question": "Will a new Gemini flagship be released by May 31, 2026?",
             "outcomePrices": '["0", "1"]', "volume": 120451, "closed": True},
            {"question": "Will a new Gemini flagship be released by July 31, 2026?",
             "outcomePrices": '["0.415", "0.585"]', "volume": 254},
        ],
    },
    {
        # Per-day probabilities, NOT a CDF. Every question says "released on".
        "title": "Next Google Gemini Pro Model released on...?",
        "slug": "next-google-gemini-pro-model-released-on",
        "volume": 375248,
        "tags": [{"label": "Gemini"}],
        "markets": [
            {"question": "Will the next Google Gemini Pro model be released on July 29, 2026?",
             "outcomePrices": '["0.012", "0.988"]', "volume": 10068},
            {"question": "Will the next Google Gemini Pro model be released on July 30, 2026?",
             "outcomePrices": '["0.0015", "0.9985"]', "volume": 24350},
        ],
    },
    {
        # Score ladder: both rungs share December 31, so it can never be a curve.
        "title": "Next Claude Opus: Text Arena Debut?",
        "slug": "next-claude-opus-text-arena-debut",
        "volume": 199407,
        "tags": [{"label": "Anthropic"}],
        "markets": [
            {"question": "Will the next Claude Opus debut at a score of at least 1470 by December 31, 2026?",
             "outcomePrices": '["0.9945", "0.0055"]', "volume": 29681},
            {"question": "Will the next Claude Opus debut at a score of at least 1520 by December 31, 2026?",
             "outcomePrices": '["0.0185", "0.9815"]', "volume": 25669},
        ],
    },
]

RANKING_FIXTURE = [
    {
        # Biggest board on the tag, but it resolves in 3 days — too stale to show.
        "title": "Which company has best AI model end of July?",
        "slug": "best-ai-model-july",
        "volume": 8001848,
        "endDate": "2026-07-31T00:00:00Z",
        "markets": [
            {"question": "Will %s have the best AI model at the end of July 2026?" % who,
             "outcomePrices": '["0.1", "0.9"]', "volume": 5000}
            for who in "Anthropic OpenAI Google Meta Alibaba Moonshot DeepSeek Mistral".split()
        ],
    },
    {
        "title": "Which company has best AI model end of August?",
        "slug": "best-ai-model-august",
        "volume": 422212,
        "endDate": "2026-08-31T00:00:00Z",
        "markets": [
            {"question": "Will Anthropic have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.905", "0.095"]', "volume": 52160},
            {"question": "Will OpenAI have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.041", "0.959"]', "volume": 31728},
            {"question": "Will Google have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.03", "0.97"]', "volume": 40240},
            {"question": "Will SpaceXAI have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.005", "0.995"]', "volume": 44529},
            {"question": "Will Moonshot have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.004", "0.996"]', "volume": 32182},
            {"question": "Will Meta have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.002", "0.998"]', "volume": 41813},
            {"question": "Will DeepSeek have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.002", "0.998"]', "volume": 30976},
            {"question": "Will Mistral have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.001", "0.999"]', "volume": 2345},
            {"question": "Will Thinky have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.001", "0.999"]', "volume": 2488},
            # unallocated slots: p=0.5, no volume. Must never render as "50%".
            {"question": "Will Company A have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.5", "0.5"]', "volume": 0},
            {"question": "Will any other company have the best AI model at the end of August 2026?",
             "outcomePrices": '["0.5", "0.5"]', "volume": 0},
        ],
    },
    {
        # Real board, but only 3 labs priced — below MIN_ENTRIES.
        "title": "Which company's AI will first hit 1550 on Chatbot Arena in 2026?",
        "slug": "first-1550",
        "volume": 109795,
        "endDate": "2026-12-31T00:00:00Z",
        "markets": [
            {"question": "Will OpenAI be the first company to have an AI model hit 1550 on Chatbot Arena in 2026?",
             "outcomePrices": '["0.032", "0.968"]', "volume": 12878},
            {"question": "Will xAI be the first company to have an AI model hit 1550 on Chatbot Arena in 2026?",
             "outcomePrices": '["0.015", "0.985"]', "volume": 8485},
            {"question": "Will no company have an AI model hit 1550 on Chatbot Arena in 2026?",
             "outcomePrices": '["0.785", "0.215"]', "volume": 24504},
        ],
    },
]


def demo():
    today = date(2026, 7, 28)

    # 1. the missing-year parse does not resurrect last year's market
    assert rung_date("Will GPT-6 be released by December 31?") is None
    assert rung_date("Will GPT-6 be released by December 31, 2026?") == date(2026, 12, 31)
    # 2. endDate is ignored — September 30 comes from the question, not the field
    assert rung_date("Will GPT-6 be released by September 30, 2026?") == date(2026, 9, 30)
    # 3. "on" is a per-day probability, never a cumulative rung
    assert rung_date("...model be released on July 29, 2026?") is None

    data = build(LADDER_FIXTURE, RANKING_FIXTURE, today)
    names = [m["name"] for m in data["models"]]
    assert names == ["Next Mythos-Class Model", "GPT-6", "Next Claude Sonnet", "Grok 5"], names
    # the dead event, the per-day PDF and the score ladder are all gone, and
    # none of them needed a title rule to exclude — shape did it
    assert "New Gemini reasoning flagship" not in names
    assert not any("Opus" in n for n in names)

    gpt6 = next(m for m in data["models"] if m["name"] == "GPT-6")
    # 4. closed rungs dropped even when their price looks live, so the ladder
    #    starts at July despite a plausible-looking 0.44 on the June market
    assert [c["date"] for c in gpt6["curve"]][:2] == ["2026-07-28", "2026-07-31"]
    assert all(0 < c["p"] < 1 for c in gpt6["curve"][1:])
    assert gpt6["median_date"].startswith("2026-09"), gpt6["median_date"]
    assert gpt6["p25"] < gpt6["median_date"] < gpt6["p75"]
    assert gpt6["lab"] == "OpenAI" and gpt6["confidence"] == "high"

    mythos = data["models"][0]
    # 5. the CDF is non-decreasing after the clamp
    ps = [c["p"] for c in mythos["curve"]]
    assert ps == sorted(ps), ps
    assert mythos["lab"] == "Anthropic" and mythos["confidence"] == "medium"

    # 6. the labs a $1000 rung floor used to delete are present, and honestly
    #    flagged as thin
    sonnet = next(m for m in data["models"] if m["name"] == "Next Claude Sonnet")
    assert sonnet["lab"] == "Anthropic" and sonnet["confidence"] == "low"
    assert len(sonnet["curve"]) == 4  # anchor + all three thin rungs
    grok = next(m for m in data["models"] if m["name"] == "Grok 5")
    assert grok["lab"] == "xAI" and grok["confidence"] == "low"
    # one lab, several models in parallel — the thing a single row per lab hid
    assert sorted(m["name"] for m in data["models"] if m["lab"] == "Anthropic") == [
        "Next Claude Sonnet", "Next Mythos-Class Model"]

    # a curve that never reaches 50% reports a floor, not a fabricated date
    flat = build(
        [{"title": "Slowpoke released by...?", "slug": "s", "volume": 50_000,
          "tags": [], "markets": [
              {"question": "Slowpoke released by August 31, 2026?",
               "outcomePrices": '["0.05","0.95"]', "volume": 5000},
              {"question": "Slowpoke released by September 30, 2026?",
               "outcomePrices": '["0.30","0.70"]', "volume": 5000}]}],
        [], today,
    )["models"][0]
    assert flat["median_date"] is None and flat["not_before"] == "2026-09-30"
    assert flat["p75"] is None and flat["p25"] is not None
    assert flat["lab"] == "Other"  # unknown lab, still shown

    # --- leaderboard
    board = data["leaderboard"]
    # 7. the $8M July board is skipped: it resolves in 3 days. The 3-lab board is
    #    skipped as too thin. August wins on volume among what's left.
    assert board["title"] == "Which company has best AI model end of August?", board["title"]
    assert board["resolves"] == "2026-08-31"
    # 8. unallocated slots never render as a real 50% contender
    assert not any(e["p"] == 0.5 for e in board["entries"])
    assert not any("Company" in e["name"] or "other" in e["name"] for e in board["entries"])
    labs = [e["lab"] for e in board["entries"]]
    assert labs[0] == "Anthropic" and board["entries"][0]["p"] == 0.905
    assert labs == sorted(labs, key=lambda l: -dict(zip(labs, [e["p"] for e in board["entries"]]))[l])
    # 9. this is the whole point of the board: labs with no release market at all
    for missing in ("Mistral", "Meta", "DeepSeek", "Thinking Machines"):
        assert missing in labs, missing
        assert missing not in [m["lab"] for m in data["models"]]
    # 10. Polymarket's own naming is mapped through to the real lab
    assert "SpaceXAI" not in labs and "xAI" in labs
    assert "Thinky" not in labs

    # --- the crawlable copy. Its whole job is containing the words a search
    # engine matches on, so assert the words are there.
    page = static_html(data)
    assert "GPT-6" in page and "OpenAI" in page
    assert "Grok 5" in page and "(thin market)" in page
    assert long_date(gpt6["median_date"]) in page
    for missing in ("Mistral", "Meta", "DeepSeek"):
        assert missing in page, missing  # board-only labs still reach the HTML
    assert long_date("2026-08-21") == "21 August 2026"
    # a dateless model states the floor instead of inventing a median
    floor = static_html({"models": [flat], "leaderboard": None, "as_of": "2026-07-28"})
    assert "not before <strong>30 September 2026</strong>" in floor
    # names come from the API, so they are escaped, not interpolated raw
    evil = static_html({
        "models": [dict(flat, name="<script>x</script>", lab="A&B")],
        "leaderboard": None, "as_of": "2026-07-28"})
    assert "<script>" not in evil and "&lt;script&gt;" in evil and "A&amp;B" in evil

    # a duplicated marker must abort, not splice at the first hit and truncate
    # the page — this ate index.html once, so it stays pinned
    dupe = "/tmp/.wtnm-dupe-test.html"
    with open(dupe, "w") as f:
        f.write("<style>/* %s */</style>\n%s\n%s\ntail" % (STATIC_START, STATIC_START, STATIC_END))
    try:
        splice_static(data, dupe)
        raise AssertionError("duplicate marker spliced instead of aborting")
    except SystemExit as e:
        assert "need exactly 1" in str(e), e
    # ...and so must a page with no markers at all
    with open(dupe, "w") as f:
        f.write("<p>nothing here</p>")
    try:
        splice_static(data, dupe)
        raise AssertionError("marker-less page spliced")
    except SystemExit as e:
        assert "need exactly 1" in str(e), e

    # the splice is idempotent: re-running must not nest or drop the markers
    scratch = "index.html"
    try:
        with open(scratch) as f:
            before = f.read()
    except OSError:
        before = None
    if before is not None:
        try:
            splice_static(data, scratch)
            splice_static(data, scratch)
            with open(scratch) as f:
                after = f.read()
            assert after.count(STATIC_START) == 1 and after.count(STATIC_END) == 1
            assert "GPT-6" in after
        finally:
            with open(scratch, "w") as f:
                f.write(before)

    print("ok")


if __name__ == "__main__":
    sys.exit(demo() if "--test" in sys.argv else main())
