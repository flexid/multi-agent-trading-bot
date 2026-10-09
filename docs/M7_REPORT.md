# M7 report: X poster

2026-10-09. **Built, dry-run.** Lint, mypy and 115 tests pass. Nothing has been posted; no X write has been made.

## What works

| Piece | State |
| --- | --- |
| Number whitelist (`app/social/whitelist.py`) | Only prices, leverage, percentages and durations may appear as numbers; links, amounts, balances, automation and advice words fail. `site=True` additionally allows "nfa" and "bot" for the public site footer. Shared with the M8a snapshot. |
| Writer and auditor | `claude-sonnet-5-5` writes from the trade record, summarized reasons and the style profile (`app/prompts/post_writer.md` v2: plain, direct, normal capitalization per the owner); `gpt-5.6-terra` audits against six rules; the whitelist runs after both. Any failure → template. |
| Templates (`templates.py`) | 10 opening and 10 closing templates filled from the trade record; every one passes the whitelist by construction (tested). Paper trades are prefixed "Paper:". |
| Threading, delay, cap (`poster.py`) | Opening = new post, close = reply to the opening post's id; random 1–10 minute delay; above `max_posts_per_day` closes are bundled into a daily summary. Posts are queued in `x_posts_out` with audit notes and sent by the executor's tick. |
| X write client | OAuth 1.0a user context over `POST /2/tweets`, using the existing keys in `.env`. Untested against X (no post allowed yet). |
| Executor hook | Posts are queued after a fill on the primary track only; dry-run unless `posting.enabled` and (live or `post_in_shadow`). Dry-run posts are template-only, so shadow mode spends nothing on posts nobody sees. |
| Style profile (`style.py`) | `python -m app.social.style` builds `style_profile.md` from the owner's recent posts (owned reads); until run, the spec's two example posts are the reference. |

## Not yet

| Item | Waiting on |
| --- | --- |
| Style profile from real posts | Owner's OK for ~200 owned reads (~$1). |
| First real post (write path verification) | Owner's OK for a test post, or wait for go-live. |
| Posting during shadow | `post_in_shadow` stays `false` unless the owner flips it. |
| Archive check against own posts ("no sentence repeats") | Needs the style-profile reads; added with them. |

## Needed from the owner

1. OK for the ~200 owned reads of @decentradork.
2. A test post (and its text) or dry-run until go-live.
3. `post_in_shadow`: `false` or `true`.
