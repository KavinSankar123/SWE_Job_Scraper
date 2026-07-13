# dist/ — sharing the tech watcher

Packages [`../tech/tech_watcher.py`](../tech/tech_watcher.py) into a zip that someone else
can unzip, set up, and run on their own machine with their own email.

## Build it

```bash
./dist/build_zip.sh                 # -> ~/Downloads/tech-job-watcher.zip
./dist/build_zip.sh /path/out.zip   # custom destination
```

It always packages the **current** `tech/tech_watcher.py`, so you can never hand out a
stale copy — rebuild and re-send any time you change the script or add a company.

### It refuses to build if anything sensitive would ship

The script hard-fails if the staged archive contains a real-looking app password, a
launcher (`run.sh` / `run_tech.sh`), a database, or a log. **Never** put your own
`run_tech.sh` in here — it holds your Gmail app password, and sending it would give the
recipient full send-access to your account. They create their own from the template.

## What's in the zip

`package/` **is** the zip's contents, plus `tech_watcher.py` copied in at build time:

| File | Purpose |
|---|---|
| `package/setup.sh` | One command: builds the venv, installs deps, creates their `run_tech.sh`, self-tests |
| `package/run_tech.sh.example` | Credential template — **they** fill in **their** Gmail app password |
| `package/README.md` | The recipient-facing guide (setup, usage, troubleshooting) |
| `package/requirements.txt` | Just `requests` |
| `tech_watcher.py` | Copied from `../tech/` at build time |

Editing what the recipient sees = editing `package/`.

## What to tell them

> 1. Unzip, then `cd tech-job-watcher && ./setup.sh`
> 2. Put your Gmail address + an **App Password** into `run_tech.sh`
>    (<https://myaccount.google.com/apppasswords> — needs 2-Step Verification on)
> 3. `./run_tech.sh --preview` to see what you'd get, then
>    `./run_tech.sh --once --notify-seed` to scrape and email everything open right now.

⚠️ On a **fresh install the database is empty**, so `--email-db` has nothing to send.
`--once --notify-seed` is the command that scrapes *and* emails the current batch.
