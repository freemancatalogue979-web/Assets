# VeriPhone Account Manager

A single-file PyQt6 desktop dashboard that runs a **sequential, authorized**
browser-based account-registration test suite against a staging environment,
using the **Browser Use Cloud API (SDK v4)**.

```
python main.py
```

> **Authorized testing only.** Point `TARGET_URL` at a staging domain you own
> and are permitted to test. The application deliberately does **not** attempt
> to solve, bypass or retry CAPTCHAs, e-mail/phone verification, rate limits or
> any other account protection — such a task is parked as
> `WAITING_FOR_USER` and handed back to the operator.

---

## 1. Setup

```bash
python -m pip install PyQt6 browser-use-sdk
python main.py
```

* Python 3.10+ (tested on 3.11 and 3.12)
* Linux note: PyQt6 needs the usual Qt system libraries, e.g.
  `sudo apt install libxkbcommon-x11-0 libegl1 libgl1 libxcb-cursor0`
* If PyQt6 cannot be imported, `main.py` prints an actionable install message
  instead of a traceback.

## 2. Configuration

Everything lives in the configuration block at the top of `main.py`:

| Setting | Purpose |
| --- | --- |
| `BROWSER_USE_API_KEY` | Browser Use Cloud API key (`X-Browser-Use-API-Key`) |
| `TARGET_URL` | Authorized staging signup page |
| `DEFAULT_PASSWORD` | Test-account password (prefills the password field) |
| `OUTPUT_FILE` / `KEY_OUTPUT_FILE` | `results.csv` / `key.txt` |
| `BROWSER_USE_MAX_COST` | Per-task cost ceiling in USD (`maxCostUsd`) |
| `OUTPUT_DIR` | Optional absolute folder for the output files |
| `BROWSER_USE_MODEL` | Optional model override (empty = SDK default) |
| `TASK_TIMEOUT_SECONDS` | Watchdog per e-mail task (default 600 s) |
| `POLL_INTERVAL_SECONDS` | Run-status poll interval |
| `DELAY_BETWEEN_TASKS_SECONDS` | Pause between two consecutive tasks |
| `MAX_TASK_RETRIES` | Retries for *recoverable* failures only |
| `PROXY_COUNTRY_CODE` | Browser Use proxy region (`""` disables the proxy) |

For local secrets you can also drop a `_overrides.local.py` next to `main.py`
(git-ignored); only names already defined in the config block are accepted:

```python
BROWSER_USE_API_KEY = "bu_live_..."
DEFAULT_PASSWORD = "Staging!Pass9"
```

## 3. Using the app

1. **Upload TXT…** — one e-mail address per line. Empty lines, duplicates
   (case-insensitive, first occurrence wins) and malformed addresses are
   skipped and reported; the original order is preserved.
2. Confirm the **test password** (prefilled from `DEFAULT_PASSWORD`, with a
   Show/Hide control).
3. **START** — tasks run strictly one at a time. A new browser run is never
   created while a previous run is alive.
4. **PAUSE / RESUME / STOP** — Pause is applied between tasks (the v4 API has
   no run-pause endpoint); if pressed mid-run the finished task is parked as
   `WAITING_FOR_USER` instead of silently continuing. Stop cancels the active
   run through `runs.cancel` and marks queued addresses `CANCELLED`.
5. Watch **Live activity** (timestamped, colour-coded) and the **Task queue**
   tab — double-click a row to reopen its Browser Use live view.

Statuses: `PENDING`, `RUNNING`, `WAITING_FOR_USER`, `COMPLETED`, `FAILED`,
`CANCELLED`.

### When a task parks as `WAITING_FOR_USER`

The app never bypasses a protection, so it explains whichever blocker it hit
instead of assuming a CAPTCHA:

| Blocker | What the operator is told |
| --- | --- |
| `captcha` | Solve the challenge yourself in the live view, then re-run the address |
| `email_verification` | Confirm the test inbox / mail fixture for that address, then re-run it |
| `phone_verification` | Use the approved test number / fixture, then re-run it |
| `rate_limit` | Wait for the limit to reset, then re-run it |
| anything else | Review the notice, resolve it via the approved test path, re-run |

The agent reports the blocker through the `blocker` field; free-text notices
(such as “Email not verified”) are also recognised. The parked task keeps its
`WAITING_FOR_USER` row, and **START** re-queues it (together with `FAILED` and
`CANCELLED` rows) without touching completed results.

## 4. What a task actually does

`BrowserUseService.create_run()` posts to `POST /api/v4/runs` with:

* a fixed QA instruction (signup → validation → submit → dashboard check →
  test-credential panel) that explicitly forbids bypassing protections,
* an `outputSchema` so each run returns structured fields
  (`signup_completed`, `dashboard_reached`, `account_email`,
  `test_credential`, `human_verification_required`, `blocker`,
  `failure_reason`, `checks`),
* a `maxCostUsd` ceiling,
* a `secretBindings` entry for the password, restricted to the target host —
  the password is never inlined in the task text,
* an optional proxy region.

Monitoring then polls `GET /runs/{id}/status`, streams
`GET /runs/{id}/events` into the activity log and finally reads
`GET /runs/{id}` for the result. Stop → `POST /runs/{id}/cancel`.

> The default model is whatever the account uses. If `browserSettings` needs a
> different country/see the [Browser Use API docs](https://docs.browser-use.com).
> If your workspace requires an explicit model, set `BROWSER_USE_MODEL`, e.g.
> `"claude-sonnet-5"`.

## 5. Output files

`results.csv` — appended, never truncated. Rows are keyed by e-mail address,
so a later result replaces the earlier row for the same address (no
duplicates) while other rows keep their order:

```
email,status,task_id,created_at,error_message,session_id,live_url,
account_email,credential,cost_usd,duration_seconds,attempt
```

`credential` is **masked** in the CSV. Full approved test credentials go to
`key.txt`, one `email<TAB>credential` pair per line (`chmod 600` on POSIX).

Both files are written atomically (temp file + `os.replace`), so an
interrupted run cannot corrupt previous results. On reload, previous results
are restored and already-finished addresses are skipped — use **Clear
completed** to re-run them.

Export: **Export CSV…** writes a copy anywhere, **Open folder** reveals the
output folder, **Clear completed** removes `COMPLETED` rows and their
credentials.

## 6. Error handling

| Situation | Behaviour |
| --- | --- |
| Invalid / duplicate / empty lines | Skipped and summarised in the log |
| Missing or unreadable TXT | Dialog + log entry, queue untouched |
| Placeholder config, invalid API key | Blocked before any run, actionable message |
| 401/403 from the API | Task `FAILED`, not retried |
| 402 spend limit | Task `FAILED` with a top-up hint |
| 429/5xx/network hiccup | Recoverable: retried (`MAX_TASK_RETRIES`) |
| Status polling keeps failing | Bounded by the task watchdog, run cancelled |
| Task timeout | Run cancelled, task `FAILED` (recoverable) |
| CAPTCHA / verification / e-mail check / rate limit | `WAITING_FOR_USER` with blocker-specific guidance, live view linked |
| Dashboard check fails | `FAILED` with the agent's reason |
| `results.csv`/`key.txt` locked | Log + dialog explaining what to close |

A failing task never stops the queue, and the GUI never blocks: all network
work happens in `QThread` workers that talk to the UI through Qt signals.

## 7. Architecture (single file)

```
configuration          ← edit here
helpers · import · EmailTask · ResultStore   (pure Python, testable)
BrowserUseService + v4 SDK wrapper           (pure Python, testable)
ConnectionCheckThread · QueueWorker          (QThread + pyqtSignal)
theme · Card/StatCard/StatusPill             (Qt widgets)
MainWindow · run()                           (dashboard)
```

Statuses and CSV columns are enums/constants, and every outward-facing
operation (run creation, polling, cancellation, persistence) is a small
method, so the app is straightforward to extend — extra checks per task,
another export format, or a different dashboard contract.
