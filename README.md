# trackFlyPrice

Watches round-trip flight prices for one route across a range of departure /
return dates, stores every observation in SQLite, sends a **Signal** message
when a date pair hits a new all-time low, and serves a small HTML price chart.

No API keys, no Docker, no database server — one Python process and two files
(`data/prices.db`, `data/trackfly.log`).

```
every N min ──► Google Flights (date pairs) ──► SQLite ──┬──► Signal on new low
                                                         └──► http://host:8099 chart
```

## Install

Needs Python 3.10+ (`python3 -m venv` must work; on Debian/Ubuntu:
`sudo apt install python3-venv`).

```bash
git clone <this repo> trackFlyPrice && cd trackFlyPrice
cp .env.example .env        # then edit it
./run.sh --once             # installs deps, one poll, no dashboard
./run.sh                    # loop + dashboard (run it in tmux)
```

```bash
tmux new -s flights './run.sh'      # Ctrl-b d to detach
```

`./run.sh --list-pairs` prints every date pair and its Google Flights URL.

## Configure (`.env`)

```dotenv
FROM=VIENNA                 # city name or IATA code
TO=COPENHAGEN
DEPARTURE_DAY_RANGE=26-30   # 26th..30th ("28-3" wraps into the next month)
DEPARTURE_MONTH=12
DEPARTURE_YEAR=2026
RETURN_DAY_RANGE=2-5
RETURN_MONTH=1
RETURN_YEAR=2027

SIGNAL_ACCOUNT=+43XXXXXXXXX
SIGNAL_RECIPIENTS=+43XXXXXXXXX,+45XXXXXXXXX
```

Everything else is optional — `.env.example` lists the keys with their defaults:

| Key | Default | What it does |
| --- | --- | --- |
| `CURRENCY` | `EUR` | ISO currency passed to Google |
| `ADULTS`, `SEAT_CLASS` | `1`, `economy` | `economy` / `premium-economy` / `business` / `first` |
| `MAX_STOPS` | – | `0` = nonstop only |
| `POLL_INTERVAL_MIN`, `POLL_JITTER_MIN` | `180`, `10` | poll cadence |
| `POLL_PAIRS_PER_CYCLE` | `0` | `0` = all pairs every cycle; `N` rotates |
| `REQUEST_DELAY_SEC` | `3` | pause between two Google requests |
| `ALERT_MIN_DROP_ABS`, `ALERT_MIN_DROP_PCT` | `1`, `0` | ignore tiny drops |
| `ALERT_COOLDOWN_H` | `3` | one alert per pair per N hours max |
| `LANGUAGE`, `SIGNAL_LANGUAGES` | `en` | `ru` switches everything; per-number `+45…:ru` |
| `WEB_PORT`, `WEB_USER`, `WEB_PASS` | `8099`, – | dashboard port / optional basic auth |
| `PUBLIC_URL` | auto | link included in replies |

## Signal

Signal has no official bot API, so this uses **signal-cli** (self-contained
Linux binary, no Java, no Docker). One-time setup:

```bash
signal-cli link -n "trackFlyPrice"   # scan the QR: Signal > Linked devices
signal-cli listAccounts              # shows the linked number
./run.sh --test-notify               # you + SIGNAL_RECIPIENTS get a test message
```

`./link-signal.sh` does the same but writes the QR to `/tmp/signal-link.png`
(the terminal QR is unreadable in many fonts).

Alternatively set `SIGNAL_MODE=http` + `SIGNAL_API_URL` for
[signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), or
`SIGNAL_MODE=none` to only log alerts.

The bot is a linked device, so it also reads messages. Send one of these to
**Note to Self** (or from a number in `SIGNAL_RECIPIENTS`):

| Message | Answer |
| --- | --- |
| `stats` | lowest now, all-time low, 3 cheapest pairs, dashboard link |
| `stats nonstop` / `stats 1` | only nonstop / at most 1 stop |
| `stats date` | sorted by departure date instead of price |
| `help` | the command list |

Russian works too: `цены`, `статы`, `помощь`, `цены без`, `цены дата`.
Messages you send to other people are ignored, and the bot never answers itself.

## Alerts

A message is sent when a pair's price is **strictly below its previous
all-time low**, the drop is at least `ALERT_MIN_DROP_ABS` *and*
`ALERT_MIN_DROP_PCT`, and the pair has not alerted in the last
`ALERT_COOLDOWN_H` hours. The first observation never alerts.

```
✈️ New lowest price: VIE → CPH
Sat 27 Dec 2026 → Sat 3 Jan 2027
EUR 161 round trip  (was EUR 178, −9.6%)
🏆 Lowest for the whole trip window
Austrian, Lufthansa · nonstop · 1h45
https://www.google.com/travel/flights/search?tfs=…
```

## Dashboard

`http://<host>:8099/` — current low, all-time low, price history chart, and a
table of all date pairs with **filters** (stops, airline) and sortable columns.
Refreshes every 60 s.

Keep the port closed and use `ssh -L 8099:localhost:8099 user@host`, or set
`WEB_USER`/`WEB_PASS`.

## Files

```
run.sh                make a venv, install, run
link-signal.sh        link this machine as a Signal device (QR png)
restart.sh            restart the tmux session after editing .env
trackfly/config.py    .env parsing, date-range expansion
trackfly/flights.py   Google Flights query + payload parser
trackfly/storage.py   SQLite schema, quotes, bests, alerts, series
trackfly/notify.py    message building + Signal delivery
trackfly/inbox.py     answers stats/help messages
trackfly/i18n.py      en/ru texts
trackfly/web.py       dashboard server + JSON API
trackfly/static/      the HTML page
```

## Notes

- Google Flights is scraped (`?tfs=` protobuf + the page's embedded JSON), so
  results occasionally come back empty or rate-limited. If that becomes
  frequent, raise `POLL_INTERVAL_MIN` or lower `POLL_PAIRS_PER_CYCLE`.
- Prices are round-trip totals for one adult as Google returns them; bags and
  seat selection are extra. Stops/duration describe the outbound leg.
- The parser reads Google's internal payload and may need an update if Google
  changes it.

