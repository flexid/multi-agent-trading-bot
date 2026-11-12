# dorkbot: review and rethink, November 2026

Written 2026-11-12 for the owner. It reviews the trading bot from the ground up on
everything it has logged since the record line of 12 October, on the candle history in
the database, and on public daily candles back to 2018, and it proposes a different
design. Nothing in the running bot was changed for this document.

The evidence is reproducible: `python -m scripts.review_evidence` on the server (parts
a, b, c) and `uv run python -m scripts.review_history` anywhere (public candles, no key).

## The short version

1. **The bot was right about the direction and lost anyway.** 92% of its trades were
   shorts while the six assets fell about a fifth. Its book lost 1.9%.
2. **The managers add no timing.** Every seat's calls did worse than a short opened at
   an average moment, at every horizon from four hours to seven days.
3. **The inputs carry no information at the bot's horizon.** The indicators score, the
   input the managers lean on most, is noise on eleven months of history.
4. **The horizon is the problem.** Rules that bet on the next day lose after fees in
   every era since 2018. Rules that follow a trend of several weeks earn in every era.
5. **The 2% take-profit is incompatible with any signal that works.** Put on a trend
   signal that grew 68% a year since 2018, it turns it into minus 83% a year.
6. **The costs equal the whole result.** Before fees the month's trading made +22.
   Exchange fees were 216 and the model bill was 211.
7. **The goal of 1% a day is out of reach by a factor of ten to twenty** for the best
   design this evidence supports.

The proposal: make dorkbot a slow book that follows multi-week trends by rule, once a
day, with no take-profit, and put the three AI managers on a challenger book where they
have to beat that rule before they get capital.

## 1. What happened: the month in numbers

Primary paper book, trades opened since 12 October 19:33 UTC:

| | |
|---|---|
| Trades closed | 75, of which 69 shorts |
| Won | 45 (60%) |
| Average win / average loss | +1.94% / -3.16% of margin |
| Winners needed to break even | 62% |
| Result | -194 on 10,000 |
| Exchange fees paid | 216 |
| Result before fees | +22 |
| Model bill, same 30 days | 211 |

By exit: 50 takes made +808, 12 stops lost -810, 12 time exits lost -227, one target
made +34.

The funnel behind those 75 trades: 2,192 asset decisions, 341 two-of-three calls, 258
approved by the risk engine. The three manager seats cost 171 of the 201 dollars spent
on models in the last 30 days.

## 2. Finding: right direction, wrong structure

The six assets, held from 12 October, lost 19%. A short of all six made 19%.

The book was on average 2,305 net short on 10,000, so 23% of equity. Holding that
short passively through the month earns about +4.4%. The book made -1.9%. The gap,
about six points of equity in a month, is timing: the book was short during the bounces
and flat during the falls.

The calls themselves show the same thing. "Drift" is what a short opened at an average
moment earned; "skill" is the call minus the drift.

| Who | Calls | Short | 1 day | drift | skill | 7 days | drift | skill |
|---|---|---|---|---|---|---|---|---|
| Consensus (2 of 3) | 334 | 97% | +0.34% | +0.64% | **-0.30** | +1.90% | +3.51% | **-1.61** |
| Fable seat | 568 | 94% | +0.03% | +0.61% | **-0.57** | +1.42% | +3.34% | **-1.92** |
| Astra seat | 278 | 96% | +0.24% | +0.63% | **-0.39** | +2.04% | +3.43% | **-1.39** |
| Grok seat | 209 | 97% | +0.45% | +0.67% | **-0.22** | +1.41% | +3.61% | **-2.20** |

Every seat, every horizon: negative. The managers call a short after the price has
fallen, which this month was just before it bounced. All of the calls' positive return
is the market's fall, which a rule that reads "the trend is down" captures without them.

The trades are worse than the calls because the take-profit forces a new entry
decision every day or so. The same 70 entries simply held for a day, with no stop and
no take, lost 0.14% on average and were right 39% of the time.

Caveat: one month, overlapping calls, three seats reading the same pack. This is not
proof that the seats can never time a market. It is a complete absence of evidence that
they can, in a month that paid 19% for being short.

## 3. Finding: the inputs carry no information at this horizon

Information coefficient (rank correlation between a score and the return that
followed; zero is noise, plus 0.05 is useful, negative is wrong-way), six assets since
the record line:

| Agent | 1 day | 3 days | 7 days | Note |
|---|---|---|---|---|
| indicators | -0.20 | -0.25 | -0.20 | highest weight, cited most by the managers |
| chart_patterns | +0.02 | +0.08 | -0.15 | |
| polymarket | -0.11 | -0.01 | -0.02 | |
| x_sentiment | -0.11 | -0.13 | -0.06 | |
| macro | +0.01 | -0.20 | -0.51 | score barely moves (standard deviation 0.015) |
| catalysts | -0.06 | -0.32 | -0.56 | ENA only, 32 asset-days |

With six correlated assets over a month the error on each of these is about 0.1 to
0.15, so a month cannot tell wrong-way from noise. The long history can, for the one
score that is pure code. The indicators score recomputed with the agent's own code on
4-hour candles:

| Period | Bars | IC 1 day | IC 3 days | Following it, a year | Fading it, a year |
|---|---|---|---|---|---|
| Eleven months before the bot | 8,412 | -0.02 | -0.05 | -11% | -78% |
| The bot's life | 1,080 | -0.22 | -0.26 | | |

So the indicators score is noise, not an inverted signal. The memo's proposal to invert
it would have been fitting one month. The memo's one "positive" agent, macro, is a
score that almost never changes; its coefficient is an accident of a small sample.

## 4. Finding: the horizon is the problem

The same few textbook rules, after 0.1% a side and borrow cost, on public daily candles
from 2018 (BTC, ETH, XRP from 2018, DOGE from 2019, SOL from 2020, ENA from 2024, equal
weight). Sharpe is return per unit of risk; above about 0.7 is good for a single
strategy. The years 2018 to 2023 had not been looked at by anyone here before the rules
were fixed.

| Rule | 2018-19 | 2020-21 | 2022-23 | 2024-26 | 2018-23, unseen |
|---|---|---|---|---|---|
| Hold everything | -0.34 | 2.36 | 0.21 | 0.45 | 0.93 |
| Follow yesterday's big move for a day | -1.32 | -0.65 | -0.92 | -0.22 | -0.84 |
| Fade yesterday's big move for a day | 0.53 | 0.22 | 0.05 | -0.81 | 0.24 |
| Follow the 28-day trend, long and short | 0.25 | 2.06 | 0.97 | 0.72 | 1.20 |
| Trend ensemble (14, 28, 56 days), long/short, volatility-scaled | 0.72 | 2.31 | 0.82 | 0.74 | 1.29 |
| Trend ensemble, long or cash, volatility-scaled | 0.64 | 3.09 | 1.14 | 1.08 | 1.73 |
| Trend ensemble, short or cash, volatility-scaled | 0.36 | -0.49 | 0.05 | 0.05 | 0.02 |

- **One-day rules lose in every era.** Following a big day loses everywhere. Fading it
  earned a little in 2018-19 and nothing since. The bot decides about the next day or
  two and exits within 2%, which is exactly where nothing survives the fees.
- **Multi-week trend earns in every era**, including the six years that were not looked
  at before the rule was written down. The volatility-scaled long/short ensemble was up
  in every calendar year: +11, +26, +81, +92, +23, +18, +33, +0.5, +18 percent for 2018
  to 2026. Its mean daily return is +0.085% with a t-statistic of 3.3 on 3,237 days.
- **Not one lucky setting.** On the last two and a half years, lookbacks of 28 and 56
  days both work, 14 is weaker, 7 and 100 do not; the ensemble includes the weak 14. Restricted to BTC and ETH, or to the
  three coins that were already the top three in 2018, the Sharpe stays near 1, so
  hindsight in the coin list does not explain it.
- **The short leg earns nothing on its own.** Over eight years, shorting downtrends
  nets about zero; the money comes from being long in uptrends and out of the market in
  downtrends. The short leg did make the difference in the last month (+7% against -3%).

What I had wrong: the "skip entries that follow a move over 1 ATR" rule that became
v15 rests on this one month. A big day was followed by a reversal of 1.6% on average in
the bot's life; over 2024 to October 2026 the yearly average ran from minus 0.2 to plus
0.5%, near zero overall. The rule is not harmful, but its measured benefit is the
month's weather.

## 5. Finding: the take-profit cannot coexist with a signal that works

The same 28-day trend signal, traded three ways, daily highs and lows, back in at the
close while the trend stands:

| Exits | 2018-19 | 2020-21 | 2022-23 | 2024-26 |
|---|---|---|---|---|
| Held while the trend holds | -6% | +406% | +47% | +29% |
| Take +2.2%, stop -4% | -86% | -92% | -64% | -81% |
| No take, stop -4% | -3% | +338% | +67% | +27% |

Growth a year. On 4-hour bars over the last year, where the order of stop and take
inside a bar is no longer an assumption, the same picture: held +3%, with the take and
an immediate re-entry -64%, with the take and a day's wait -28%, without the take -0.5%.

A trend pays through a few large moves. A 2% take sells every one of them at 2%, pays
two fees, and buys back into the same trend a little worse. The stop then gives back
4 to 6% once in a while. The replay I built last week could not see this, because it
replayed the bot's own entries, and those entries are themselves a product of the take.

## 6. Finding: the costs and the goal

- Per trade the model bill is 2.81 dollars against an average notional of 1,441: 0.20%,
  the same again as the exchange's round trip. On the paper 10,000 the two together are
  4.3% a month. On the real capital in play (185 on the pilot, about 290 on the DEX
  account) the model bill alone is close to half of that capital every month.
- The best-evidenced book in section 4 earns +0.085% a day over nine years, about
  +0.05% a day over the last four. One percent a day is twelve to twenty times that. To
  reach it by leverage, a month like its worst (-12%) wipes the account out. I can find
  no design that makes the goal reachable, and I do not think one exists.

## 7. The rethink: a slow book, with the AI outside the direction decision

### The champion: a trend book run by rule

- **Signal.** Per asset, three votes: is the price above where it was 14, 28 and 56
  days ago. The target is the average vote: fully long, a third long, a third short,
  fully short. Computed once a day after the daily close.
- **Size.** Each asset has one sixth of the equity. The position is scaled down so the
  asset contributes about 40% annualised volatility, never above its full slot. No
  leverage.
- **Exits.** The signal is the exit. No take-profit. A rebalance happens only when the
  target moves by a quarter of a slot, which is about five orders a week. The kill
  switch, the emergency brake at -25%, the liquidation guard and reconciliation stay.
- **No model in the loop.** Code computes the target and the risk engine and the
  executor do what they do today.

What that book did, after fees and borrow, with the rebalance band:

| | Long or cash | Long and short |
|---|---|---|
| Sharpe, 2018 to now / since 2022 | 1.61 / 1.15 | 1.18 / 0.82 |
| Growth a year, 2018 to now / since 2022 | 35% / 21% | 34% / 20% |
| Deepest drawdown | -19% | -27% |
| Orders a week | 4.5 | 6.5 |
| Days with a position | 70% | 100% |
| The last month (the bot made -1.9%) | -2.9% | +7.3% |
| Would hold today | cash | short all six, about 75% of equity |

A fair expectation is the recent column, not the long one: about 15 to 20% a year with
a drawdown of a fifth to a quarter, flat years like 2025 (+0.5%), and half the months
down. The edge has been shrinking since 2021 and may keep shrinking. Six assets is a
small universe. These are the honest limits of the best thing the evidence offers.

### The challenger: the three managers, on paper, against the rule

The managers keep their seats on a second paper book, one decision cycle a day instead
of nine. They are scored on two numbers that cannot be flattered by the market's
direction: skill against drift (section 2), and their book against the champion on the
same days. The promotion rule is written down before they start: positive skill at
three and seven days over at least a hundred calls on sixty days, and a book that beats
the champion after costs over three months. Until then they get no capital.

The role I would design for them is veto, not vote: a manager may cut the champion's
position in an asset for a few days with a stated reason, never add to it. That is a
decision they can be scored on, and it puts their reading of news and events where it
could plausibly help.

### What the AI still does every day

- **Guard.** The X and catalyst shock exit stays: a credible hack, delisting or depeg
  flattens that asset whatever the trend says.
- **Voice.** The posts and the weekly memo. With five orders a week the posts become
  fewer and each one has something to say.
- **Research.** The memo may propose a rule change, and every proposal carries its
  nine-year table before it ships.

### What it costs

| A month, on a 10,000 book | Now | Slow book |
|---|---|---|
| Exchange fees | 216 | 13 to 29 |
| Models | 211 | about 30 with the daily challenger, under 10 without |

### What changes around it

- **The public record** starts a new line when the champion becomes the main book. The
  site's managers page becomes the challenger scoreboard: can the AIs beat the rule.
- **The DEX mirror** follows the champion. It stays paused until the champion has run
  two clean weeks on paper, and resumes on the owner's word. A slow rule-based record
  is also the more credible basis for the vault.
- **The arena** is unchanged. Season two already moved to slow candles for the same
  reason.
- **The goal** is restated as something the evidence can carry: beat cash and beat
  holding BTC over any twelve months, with a drawdown under a quarter.

## 8. What I would change in how we work

- Fifteen logic versions in thirty days, most of them tuned on a week of trades. v11
  and v12 chased leverage the record never supported; v15 rests on one month's
  reversals. I proposed several of these.
- From here a change ships only with the long-history table where one applies, the
  skill-against-drift number, and a control book.
- The weekly memo reports skill against drift and cost as a share of equity, not hit
  rates and one-day coefficients on overlapping samples.

## 9. Decisions for the owner

1. Make the slow trend book the main book. My recommendation: yes.
2. Long or cash, or long and short. Long or cash has the better record in three of four
   eras and the smaller drawdown, and it sits in cash through a bear market like this
   one. Long and short stays active both ways; its short leg has earned nothing on
   average and made 7% this month. My recommendation: long or cash as the champion, the
   short leg as a second paper book until it earns its place.
3. Restate the goal as in section 7.
4. Keep the managers as the daily challenger at about 25 dollars a month. My
   recommendation: yes; it is the cheapest honest test of whether they can add anything.

If the answer to the first is yes, the build is small because the risk engine, the
executor, the ledgers, the paper fills, the site and the control-book plumbing are all
reused: a pure function for the targets with tests against these tables, a daily rule
cycle that writes its decisions the way the managers' cycle does, the champion on its
own paper book beside the current one for two weeks to prove the plumbing (not the
edge; the edge is the nine years), then the switch and the new record line.

## Limits of this review

- The trend result is a backtest: daily closes, 0.1% a side, 4% borrow, no slippage
  model beyond that. At this size on these six coins that is close to reality, but it
  is not a live record.
- The coin list is today's. The checks on BTC and ETH alone and on the 2018 top three
  argue against hindsight, they do not remove it.
- The findings about the managers rest on one month. The findings about horizons and
  exits rest on nine years.
