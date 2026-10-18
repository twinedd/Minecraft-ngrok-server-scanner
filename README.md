# Minecraft ngrok Server Scanner

[![Tests](https://github.com/twinedd/Minecraft-ngrok-server-scanner/actions/workflows/tests.yml/badge.svg)](https://github.com/twinedd/Minecraft-ngrok-server-scanner/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Discord](https://img.shields.io/badge/Discord-Pycord-5865F2?logo=discord&logoColor=white)

A Discord bot for discovering Minecraft Java servers on ngrok endpoints, checking their status, and keeping a history of observed player names.

## Features

- Paginated server browser showing endpoints that passed their latest check.
- Server cards with MOTD, version, online count, **First seen**, and **Last seen**.
- **Refresh** button to ping an endpoint and update the same message.
- Persistent player history with first/last observation times, five names per page, and numbered player buttons.
- Java profile lookup and skin faces, with caching and API error handling.
- Separate archived records when an address starts returning a different description or version.
- Atomic JSON writes, synchronized database access, and optional webhook command logs.

## Quick start

Requires Python **3.12**, Pipenv, and a Discord bot application.

```powershell
git clone https://github.com/twinedd/Minecraft-ngrok-server-scanner.git
cd Minecraft-ngrok-server-scanner
py -m pip install pipenv
py -m pipenv sync
Copy-Item .env.example .env
```

Edit `.env` with your own values:

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Bot token from the Discord Developer Portal |
| `BOT_OWNER_ID` | Yes | Numeric Discord user ID allowed to start/stop scanning |
| `DISCORD_WEBHOOK_URL` | No | Command log webhook; leave empty to disable |

Pipenv loads `.env` automatically. Never commit it. For direct Python execution, supply these variables in the process environment instead.

In the [Discord Developer Portal](https://discord.com/developers/applications), create a bot, enable the privileged gateway intents (the current code requests all intents), and invite it with the `bot` and `applications.commands` scopes. Allow **View Channels**, **Send Messages**, **Embed Links**, and **Attach Files**.

Start the bot:

```powershell
py -m pipenv run py ngrok_bot.py
```

On Windows, you can also double-click **start_bot.bat**. If your `py` launcher cannot locate Python, use `python -m pipenv run python ngrok_bot.py`.

## Commands and buttons

| Action | Behavior |
| --- | --- |
| `/start_scanning` | Owner only: start repeated scanning passes |
| `/stop_scanning` | Owner only: prevent the next pass; queued checks still finish |
| `/servers` | Browse servers whose latest check succeeded |
| Numbered server button | Open a server card |
| **Refresh** | Recheck the address; a five-second cooldown applies per card |
| **Player History** | Browse observed names, five per page |
| Numbered player button | Show observation times, profile lookup, and skin face |

UI text is English. Discord timestamps use the viewer's locale. Previously sent messages are snapshots; open a new list or press **Refresh** for updated server information.

## Scanning and persistence

Each pass checks ports **10000–29999** on hosts **1–7.tcp.eu.ngrok.io**: 140,000 endpoints, processed by 13 worker threads with a 0.3-second query timeout. One coordinator creates each pass after all workers finish. There is currently no global requests-per-second limit or pause between passes. Use only where you have permission to scan and respect the provider's limits.

`server_database.json` is created beside the script and is excluded from Git. Failed checks mark existing records unavailable without deleting them. **Last seen** is the latest successful reply, not the latest attempt. Legacy records without an availability flag remain hidden until checked successfully.

Player history records names returned by the server, not every player who has ever joined. Empty samples do not erase history. Changes to the normalized MOTD text or version create a new instance; older instances remain in `previous_servers`. Colors and whitespace are ignored. This is a heuristic: identical servers can be confused, and an owner's MOTD/version update can create a separate instance.

Profile lookup uses Mojang; faces use [MCHeads](https://mc-heads.net/). A matching profile or skin does not establish that the observed player authenticated as that account. Profile successes are cached for 15 minutes, missing profiles for 5 minutes, and temporary failures for 30 seconds. Faces are cached for 20 minutes. HTTP 429 from Mojang triggers a cooldown.

## Known limitations

- Stop does not cancel the current queue. Restarting before it drains can overlap scanning runs.
- Availability reflects the last check and can become stale during a long pass.
- Button views expire after 500 seconds. Reopen `/servers` when controls expire.
- Discord must receive an initial interaction response promptly. Expired player clicks are logged with their age; they cannot be recovered by retrying the same interaction.
- External APIs may be unavailable; skin failures do not remove player history.

## Development

```powershell
py -m pipenv run python -m unittest -v test_ngrok_bot
```

Tests use temporary databases and mocked network calls; they do not start the bot or scan endpoints. GitHub Actions runs the same suite on Python 3.12 using `Pipfile.lock`.

| File | Purpose |
| --- | --- |
| `ngrok_bot.py` | Discord UI, scanning, storage, and profile/skin clients |
| `lib_mc2.py` | Minecraft formatting to ANSI conversion |
| `test_ngrok_bot.py` | Regression tests |
| `Pipfile.lock` | Reproducible dependency versions |
| `.env.example` | Configuration template without secrets |

## License

See [LICENSE.md](LICENSE.md).
