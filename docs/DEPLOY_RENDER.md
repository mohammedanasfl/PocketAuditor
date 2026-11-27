# Deploy on Render (free, no credit card)

What runs where — all free:

| Part | Where |
|---|---|
| The bot (Python) | **Render** free web service |
| Database | **Neon** free Postgres |
| AI | **Gemini** API (Google AI Studio key) |
| Scheduled jobs + keep-alive | **GitHub Actions** (`.github/workflows/scheduled-jobs.yml`) |

Render's free plan sleeps after 15 minutes without traffic. The bot runs in
**webhook mode**, so a Telegram message wakes it (the first reply after a
sleep takes up to ~1 minute). The scheduled-jobs workflow also pings it every
10 minutes to keep it awake; one always-awake service fits in Render's free
750 hours a month.

## Already running v1 on Render? Upgrade the existing service

Don't create a second service from the Blueprint — two services would fight
over the same Telegram bot (each registers its own webhook). Instead:

1. **Set the new environment variables** on the existing service
   (Render → your service → **Environment**), *before* the v2 code deploys:

   | Variable | Value |
   |---|---|
   | `ALLOWED_CHAT_IDS` | your Telegram chat id (from **@userinfobot**). **Required** — if it's empty, v2 answers nobody |
   | `LLM_PROVIDER` | `gemini` |
   | `LLM_MODEL` | `gemini-3.5-flash-lite` |
   | `GEMINI_API_KEY` | your key (if not already set) |
   | `TIMEZONE` | `Asia/Kolkata` |
   | `RUN_MODE` | `webhook` |
   | `TELEGRAM_WEBHOOK_SECRET` | keep your existing one (letters, digits, `-`, `_` only) |
   | `CRON_SECRET` | keep your existing one, or set a random value |

2. **Deploy `main`.** If auto-deploy is on, the merge already triggered it —
   check **Events**. Otherwise: **Manual Deploy → Deploy latest commit**.
3. **Migrations:** the Docker image's start command and the `render.yaml`
   start command both run `alembic upgrade head` on every start, so the two
   new migrations apply automatically. If your service uses a custom start
   command without it, run `alembic upgrade head` once (Render → **Shell**,
   or from your laptop with the production `DATABASE_URL`).
4. **Webhook:** v2 registers it by itself on startup — look for
   `webhook registered at ...` in **Logs**. No manual `setWebhook` needed.
5. Continue with [step 3](#3-turn-on-the-scheduled-jobs-github) (scheduled jobs).

The rest of this guide is for a **fresh** setup.

## 1. Collect these values first

| Value | How to get it |
|---|---|
| `DATABASE_URL` | Neon dashboard → your project → connection string. Rewrite it to `postgresql+asyncpg://USER:PASSWORD@HOST/DBNAME?ssl=require` (add `+asyncpg`, replace `sslmode=require&channel_binding=require` with `ssl=require`). |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` (or `/token` for an existing bot). |
| `TELEGRAM_WEBHOOK_SECRET` | Any random text of letters, digits, `-` and `_`. Make one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GEMINI_API_KEY` | https://aistudio.google.com → Get API key. |
| `ALLOWED_CHAT_IDS` | Your Telegram chat id: message **@userinfobot** in Telegram, it replies with your id. |

## 2. Create the service on Render

1. Sign up at https://render.com with your GitHub account (no card needed).
2. Dashboard → **New** → **Blueprint** → choose the `PocketAuditor` repository.
   Render reads `render.yaml` from the repo.
3. Render asks for the values from step 1 — paste them in. (`CRON_SECRET` is
   generated for you.)
4. Click **Apply**. The first build takes a few minutes. Every push to `main`
   deploys again automatically.
5. Open the service → **Logs**. You should see
   `webhook registered at https://<name>.onrender.com/telegram/webhook`.

## 3. Turn on the scheduled jobs (GitHub)

1. Render → your service → **Environment** → copy the value of `CRON_SECRET`.
2. GitHub → repository → **Settings → Secrets and variables → Actions** →
   **New repository secret**, twice:
   - `APP_URL` = your Render address, e.g. `https://pocketauditor.onrender.com`
   - `CRON_SECRET` = the value you copied
3. GitHub → **Actions → Scheduled jobs → Run workflow** → `ping`. It should
   finish green. From now on it runs on its own:

| When (India time) | What |
|---|---|
| every 10 minutes | keep-alive ping |
| daily 08:30 | budget alerts, salary + money alerts |
| Mondays 09:30 | reconcile anything pending, weekly digest |
| 1st of the month 10:30 | monthly audit |

## 4. Try it

In Telegram, send your bot:
1. `/start`
2. `/myaccount savings 4321` (your savings account's last 4 digits)
3. forward a salary SMS, a transfer-to-savings SMS and a spend SMS
4. `/plan`
5. "set my food budget to 4000" → tap **Yes**

## If something doesn't work

| Symptom | Fix |
|---|---|
| Bot says "This bot is private. Your chat id is …" | Put that id in `ALLOWED_CHAT_IDS` on Render (Environment) — it redeploys. |
| No reply at all | Render → Logs. A warning about `PUBLIC_URL or TELEGRAM_WEBHOOK_SECRET` means the secret is missing. |
| `setWebhook` error in the logs | The webhook secret has a character Telegram doesn't allow — use only letters, digits, `-`, `_`. |
| Database errors at startup | Check the `DATABASE_URL` shape in step 1 (`+asyncpg`, `?ssl=require`). |
| Scheduled jobs stopped | GitHub pauses scheduled workflows after 60 days without repository activity — re-enable it under Actions. |
