# JartexNetwork Clan Trophy Tracker (multi-clan)

A Discord bot that polls the JartexNetwork public API every 5 minutes for
**any number of clans**, detects trophy gains/losses for each, and
automatically posts updates to a Discord channel. Clans can be added or
removed on the fly with slash commands - no restart needed. There are no
manual lookup commands; the bot only posts when it detects a change.

## How it works

Since JartexNetwork doesn't expose a live event stream for wins/losses, this
bot **polls** the clan stats endpoint on a timer and diffs the trophy count
against the last known value. A trophy increase is treated as a "win" event,
a decrease as a "loss" event. This is the standard approach used by
community bots built on this API (there's no way to subscribe to real-time
win/loss events unless you have access to the server's own plugin code).

## Setup

1. **Create the bot application**
   - Go to https://discord.com/developers/applications
   - New Application -> Bot -> copy the token
   - Under "Privileged Gateway Intents" you don't need any special intents
     for this bot (it doesn't read message content)
   - Generate an OAuth2 invite URL with scopes `bot` + `applications.commands`,
     permission `Send Messages` (and `Embed Links`), and invite it to your server

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Then fill in:
   - `DISCORD_TOKEN` - your bot token
   - `ANNOUNCE_CHANNEL_ID` - right-click a channel in Discord (Developer Mode
     must be on) -> Copy Channel ID
   - `CLAN_NAMES` - (optional) comma-separated clans to seed on first run,
     e.g. `ClanA,ClanB`. You can also skip this and just add clans later
     with `/track_clan` once the bot is running.

4. **Run it**
   ```bash
   python bot.py
   ```

## Restricting which servers can use this bot

Two layers, use both for best results:

**1. Turn off public installability (stops randoms from even generating an invite link)**
1. Go to https://discord.com/developers/applications -> your app -> **Installation**
2. Under **Installation Contexts**, disable install options you don't need, and/or
3. Go to **Bot** page -> turn **OFF** "Public Bot"
   - With this off, only you (the application owner/team) can generate a valid OAuth invite URL for the bot. Anyone else trying to invite it gets blocked.

**2. Code-level allowlist (backup, in case it's ever added anyway)**
- Set `ALLOWED_GUILD_IDS` in `.env` to a comma-separated list of the Discord server IDs you approve (right-click a server icon -> Copy Server ID, with Developer Mode on)
- If the bot is added to any server not on that list, it automatically posts a notice ("This bot is private...") and leaves immediately
- On every startup, it also sweeps all servers it's currently in and leaves any that aren't approved - so if you tighten `ALLOWED_GUILD_IDS` later, just restart the bot to enforce it
- Leave `ALLOWED_GUILD_IDS` empty to disable this restriction entirely (not recommended if you're trying to keep this private)

Within an approved server, all members can still use `/track_clan`, `/untrack_clan`, and `/list_clans` freely - this only restricts *which servers* the bot will operate in, not who can use commands inside an approved one. Let me know if you'd also like commands restricted to specific roles/users within a server.

## Commands

- `/set_command_channel` - **(requires Administrator permission)** restricts `/track_clan`, `/untrack_clan`, and `/list_clans` to only work in the channel this is run in. No need to look up a channel ID - just run it in the channel you want.
- `/clear_command_channel` - **(requires Administrator permission)** removes that restriction, allowing the management commands in any channel again.
- `/track_clan <clan_name> [channel]` - start tracking a clan. Optionally pass a channel to route that clan's updates there specifically; if omitted, it uses the default `ANNOUNCE_CHANNEL_ID`. Running this again for a clan that's already tracked updates its channel.
- `/untrack_clan <clan_name>` - stop tracking a clan
- `/list_clans` - show every clan currently being tracked and which channel it posts to

There are no manual "check trophies now" commands - the bot posts to the
relevant channel automatically whenever it detects a trophy change during
its 5-minute poll.

**Update embed layout:** a small "📈 Gain" / "📉 Loss" badge above the clan
name, previous and current trophy counts side by side, and a full-width
"Change" summary (`old → new (+/-X)`) at the bottom.

**Who can change the channel restriction:** `/set_command_channel` and
`/clear_command_channel` already require the Administrator permission -
Discord enforces this itself, so a regular member can't run them at all. If
you don't want *every* Administrator in the server to be able to change
this (e.g. you have multiple admins but only trust yourself with this
setting), set `BOT_OWNER_IDS` in `.env` to your own Discord user ID (right-click
your name with Developer Mode on -> Copy User ID). With that set, only
listed users can run these two commands, even if others also have Admin.
Every use of these commands is also logged in the bot's console and posted
visibly in the channel (not silently), so there's always a trail of who
changed it.

## Notes / things you may need to adjust

- **API response shape**: the code checks a few likely field names
  (`trophies`, `guildTrophies`, `clan.trophies`) for where the trophy count
  lives in the JSON response. Run the bot once, check the logs, and adjust
  `fetch_clan_trophies()` in `bot.py` if the actual field name differs -
  JartexNetwork's API has changed shape before.
- **Rate limits**: the API returns `429` if you poll too aggressively. At 5
  minutes per cycle (with a 2s gap between clans), this is a safe interval.
  The bot still handles `429`s automatically with backoff (waits, doubles on
  repeated hits, capped at 15 minutes, resets on success) in case
  JartexNetwork ever tightens limits.
- **Hosting**: this needs to run continuously to catch changes - a small VPS,
  Railway/Render free tier, or a Raspberry Pi all work fine for something
  this lightweight.
- **Multiple clans**: each poll cycle goes through every tracked clan one at
  a time, with a 2-second gap between each request, so tracking several
  clans doesn't burst the API all at once. With a 5-minute interval this
  comfortably supports dozens of clans without needing any changes.
- **First run per clan**: the first poll after `/track_clan` only
  establishes a baseline trophy count for that clan (nothing to diff
  against yet), so you won't see a Discord post for it until the next poll
  detects a change.