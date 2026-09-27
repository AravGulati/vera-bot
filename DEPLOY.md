# Getting your public bot URL

You need a URL like `https://vera-bot-xxxx.onrender.com` where these five endpoints are live:
`POST /v1/context` · `POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata`.

## Option A — Render (recommended, ~10 minutes)

1. **Put the code on GitHub.** Create a new repository, for example `vera-bot`, at github.com/new. Then do one of the following:
   - drag and drop the unzipped project files into it with "uploading an existing file" on GitHub, or
   - push from a terminal:
     ```bash
     cd vera-bot
     git init && git add . && git commit -m "Vera+ bot"
     git branch -M main
     git remote add origin https://github.com/<you>/vera-bot.git
     git push -u origin main
     ```
2. **Create the service.** Go to dashboard.render.com, then **New → Blueprint**, connect GitHub and pick the `vera-bot` repo. Render reads `render.yaml` and fills in everything:
   - build: `pip install -r requirements.txt`
   - start: `uvicorn bot:app --host 0.0.0.0 --port $PORT --workers 1`
   - health check: `/v1/healthz`
   
   *(If you'd rather set it up by hand: New → Web Service → your repo → Runtime Python, then enter those same build and start commands.)*
3. **Set your team details.** Under **Environment**, set `TEAM_NAME`, `TEAM_MEMBERS` (comma-separated) and `CONTACT_EMAIL`. These appear in `/v1/metadata`.
4. **Wait for "Live".** It usually takes 2–3 minutes. Your URL is at the top of the service page.
5. **Avoid free-tier sleep.** A free Render instance sleeps after 15 minutes idle, and the first request after that takes about 50 seconds, longer than the judge's 30-second timeout. Do one of these:
   - switch the plan to **Starter** (about $7/month, never sleeps). This is the safest choice for the test window.
   - or keep it awake with a free monitor: at uptimerobot.com add an HTTP monitor on `https://<your-url>/v1/healthz` every 5 minutes.

## Option B — Railway (no sleep on the trial credit)

Go to railway.app, then **New Project → Deploy from GitHub repo** and pick `vera-bot`. Railway detects the `Procfile`. Then:
- **Settings → Networking → Generate Domain** to get your public URL.
- **Variables**: add `TEAM_NAME`, `TEAM_MEMBERS` and `CONTACT_EMAIL`.

## Option C — Docker (any host)

```bash
docker build -t vera-bot .
docker run -p 8080:8080 -e TEAM_NAME="..." vera-bot
```

## Verify before you submit (replace the URL)

```bash
export BOT_URL=https://vera-bot-xxxx.onrender.com
curl $BOT_URL/v1/healthz
curl $BOT_URL/v1/metadata
python scripts/local_judge.py          # full lifecycle replay against the live URL; expect "0 failed"
```

Optional: set `LLM_API_KEY` at the top of `judge_simulator.py`, then run `python judge_simulator.py` for magicpin's LLM scoring.

## Important
- **Keep exactly one worker/instance.** The bot keeps state in memory, so don't turn on autoscaling.
- **Run `POST /v1/teardown` before the real test** if you tested against the live URL. It wipes stored state, so the judge's pushes start clean.
- **Submit** the base URL only, with no `/v1` and no trailing slash, for example `https://vera-bot-xxxx.onrender.com`.
