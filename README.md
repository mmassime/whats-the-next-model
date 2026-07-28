# whats-the-next-model

When each AI lab's next model is expected, and who's ahead today, read off
Polymarket prices.

```
fetch.py  ──cron──>  data.json  ──>  index.html   (fetched by the page)
             └───────────────────>  index.html   (static copy, spliced in)
```

Three files, no backend, no dependencies. GitHub Actions runs `fetch.py` every
three hours and commits both files when a price actually moved; GitHub Pages
serves the folder.

## Two market shapes

`fetch.py` reads two of Polymarket's own tags. It matches on **shape, not on
market names**, so a renamed bet keeps working:

- **`ai-releases`** — ladders of "released by DATE?" markets. Each is a CDF:
  P(shipped by date). Crossing it at 50% gives the median expected release date,
  25%/75% the uncertainty band. Anything on the tag with at least two distinct
  future dates is a ladder; score ladders ("debut at ≥1470 **by** December 31")
  collapse to a single date and fall out on their own.
- **`ai-rankings`** — "who has the best model" boards, one market per lab. This
  is the only place most labs appear at all: Polymarket prices release dates for
  a handful of labs but ranks twenty-one of them, so Mistral, Meta, DeepSeek and
  Thinking Machines are here and nowhere else.

Only a few labs have a release ladder. That is a gap in the markets, not a
signal about the lab, and the page says so.

## Gotchas the code guards against

Each of these produced silently wrong output at some point:

- **The feed is paginated and truncates silently.** A bare `limit=100` drops the
  tail of the `ai` tag, which is exactly where the thin ladders live (Grok 5,
  Kimi K4, Claude Sonnet). Five labs were missing because of it.
- **`closed=false` filters events, not markets.** Roughly half the markets inside
  an open event are already settled.
- **`endDate` is wrong on ladder markets.** A "September 30, 2026" question ships
  `endDate: 2026-06-30`. The date is parsed from the question text instead. (It
  *is* trustworthy on ranking events, which is where the resolution date comes
  from.)
- **"released **on** DATE" is a different event from "released **by** DATE".** The
  first prices single days — a PDF. Read as cumulative it yields a confident,
  wrong median.
- **A question with no year is last year's settled market.** "released by
  December 31?" must never have a year inferred.
- **The raw ladder is not monotonic**, so a CDF has to be clamped with a running
  max.
- **Leaderboards ship unallocated slots** ("Company A" … "Company M") at p=0.5
  with no volume. Rendered as-is they read as 50% contenders.
- **Gamma 403s urllib's default User-Agent.** curl works, so this passes local
  testing and fails only in the Action.
- **`generated_at` defeats a "commit only if changed" guard.** It is a fresh
  timestamp every run, so a plain `git diff --quiet` always sees a change and
  every run commits. Harmless once a day; at every three hours it fills `git
  log` — the Phase 2 history store — with empty snapshots. The Action diffs
  with `-I'"generated_at"'` so a commit means a price moved.

`python3 fetch.py --test` pins every one of them against trimmed real API output.

## Run it

```sh
python3 fetch.py --test    # self-check, no network
python3 fetch.py           # writes data.json
python3 -m http.server     # then open localhost:8000
```

`index.html` fetches `data.json`, so it needs to be served over http — opening
the file directly won't work (the page says so if you try).

## Being found

The page draws everything from `data.json` in the browser, so the served HTML
said nothing: no lab, no model, no date — the words anyone would search for
existed only in JSON a crawler may or may not execute its way to. So `fetch.py`
also writes the numbers as plain HTML into the `static:` marker block in
`index.html`, and the script drops that block the moment the real render starts.
It doubles as the no-JS version of the page.

Two things about that splice, both learned the hard way:

- **The markers must be unique.** Writing `static:start` out in full a second
  place (a CSS comment explaining the block) made the splice cut there and take
  the rest of the file with it. `fetch.py` now counts both markers and refuses
  unless each appears exactly once.
- **`index.html` is a generated file now**, in that one region. The Action
  commits it alongside `data.json`.

There are also `og:`/`twitter:` tags, because this page's readers arrive from a
link pasted into HN, Reddit or Slack rather than from a search result, and an
unfurl with no title was a free thing to lose. No `og:image` — that needs a
generated PNG in the repo, and it can wait for a post that actually lands.

None of this makes the page rank. A new path on `github.io` with no inbound
links loses to news sites on every query that matters; distribution is a post,
not a meta tag.

## Deploy

Settings → Pages → deploy from branch, root. Actions needs read+write contents
permission (Settings → Actions → General → Workflow permissions) for the
scheduled commit to push.

## Design notes

- **Lab identity is grouping and labels, never colour.** Past three categorical
  slots no hue ordering clears the all-pairs CVD/contrast gate, and this page
  shows six labs on the timeline and twenty-one on the board. One accent hue does
  all the data ink; colour is freed to mark *thin markets* (dashed outline,
  hollow dot) — which is the thing a reader most needs not to be misled by.
- **The median is interpolated; the step curve is not.** Where a priced gap
  straddles 50%, the chart draws the interpolation as a dashed segment, so the
  median marker sits on visible reasoning instead of hovering over a step that
  reads 3% on the same date.
- **No median means no date.** A curve that never reaches 50% reports "after
  &lt;last priced date&gt;" rather than an extrapolation dressed as an estimate.
  
## What's not here

- **Movement arrows** (↗ / → / ↘, "how has this date moved since last week").
  Phase 2, and there is nothing to build: it reads out of `git log` once a
  couple of weeks of `data.json` commits exist.
- **Reddit rumours.** Phase 3, deliberately deferred.
