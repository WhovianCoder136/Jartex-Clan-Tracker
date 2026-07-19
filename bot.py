"""
JartexNetwork Clan Trophy Tracker (multi-clan)
------------------------------------------------
Polls the JartexNetwork public API every 5 minutes for EACH tracked clan's
trophy count and roster, diffs it against the last known values, and automatically
posts any changes (wins, losses, joins, leaves) to a configured Discord channel.
Clans are managed with slash commands:
/track_clan <name> [channel]  - start tracking a clan
/untrack_clan <name>          - stop tracking a clan
/list_clans                   - show all tracked clans and their channels
"""
import os
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone, time
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCE_CHANNEL_ID = os.getenv("ANNOUNCE_CHANNEL_ID")
DB_PATH = os.getenv("DB_PATH", "clan_trophies.db")
ALLOWED_GUILD_IDS = {
    g.strip() for g in os.getenv("ALLOWED_GUILD_IDS", "").split(",") if g.strip()
}
BOT_OWNER_IDS = {
    u.strip() for u in os.getenv("BOT_OWNER_IDS", "").split(",") if u.strip()
}
SEED_CLAN_NAMES = [
    c.strip() for c in os.getenv("CLAN_NAMES", "").split(",") if c.strip()
]

API_URL_TEMPLATE = "https://stats.jartexnetwork.com/api/clans/{clan_name}"
POLL_INTERVAL_SECONDS = 2 * 60
PER_CLAN_REQUEST_DELAY_SECONDS = 2
BACKOFF_INITIAL_SECONDS = 60
BACKOFF_MAX_SECONDS = 15 * 60

_current_backoff = BACKOFF_INITIAL_SECONDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("clan-tracker")

# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_clans (
            clan_name TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            channel_id TEXT
        )
        """
    )
    cur.execute("PRAGMA table_info(tracked_clans)")
    existing_columns = {row[1] for row in cur.fetchall()}
    if "channel_id" not in existing_columns:
        cur.execute("ALTER TABLE tracked_clans ADD COLUMN channel_id TEXT")
        log.info("Migrated tracked_clans table to add channel_id column.")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS command_channels (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trophy_state (
            clan_name TEXT PRIMARY KEY,
            trophies INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trophy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clan_name TEXT NOT NULL,
            delta INTEGER NOT NULL,
            trophies_after INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_members (
            clan_name TEXT,
            member_name TEXT,
            PRIMARY KEY (clan_name, member_name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_baseline (
            clan_name TEXT PRIMARY KEY,
            trophies INTEGER NOT NULL,
            level INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_baseline_members (
            clan_name TEXT,
            member_name TEXT,
            PRIMARY KEY (clan_name, member_name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_ranks (
            clan_name TEXT PRIMARY KEY,
            rank INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM tracked_clans")
    count = cur.fetchone()[0]
    if count == 0 and SEED_CLAN_NAMES:
        now = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            "INSERT OR IGNORE INTO tracked_clans (clan_name, added_at, channel_id) VALUES (?, ?, NULL)",
            [(name, now) for name in SEED_CLAN_NAMES],
        )
        conn.commit()
        log.info("Seeded tracked clans from CLAN_NAMES: %s", ", ".join(SEED_CLAN_NAMES))

    conn.close()


def add_tracked_clan(clan_name: str, channel_id: str = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur.execute(
            "INSERT INTO tracked_clans (clan_name, added_at, channel_id) VALUES (?, ?, ?)",
            (clan_name, now, channel_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        if channel_id is not None:
            cur.execute(
                "UPDATE tracked_clans SET channel_id = ? WHERE clan_name = ?",
                (channel_id, clan_name),
            )
            conn.commit()
        return False
    finally:
        conn.close()


def remove_tracked_clan(clan_name: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM tracked_clans WHERE clan_name = ?", (clan_name,))
    removed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def get_tracked_clans():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT clan_name, channel_id FROM tracked_clans ORDER BY clan_name")
    rows = cur.fetchall()
    conn.close()
    return rows


def set_command_channel(guild_id: str, channel_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO command_channels (guild_id, channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id
        """,
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()


def get_command_channel(guild_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT channel_id FROM command_channels WHERE guild_id = ?", (guild_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def clear_command_channel(guild_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM command_channels WHERE guild_id = ?", (guild_id,))
    removed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def get_last_trophies(clan_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT trophies FROM trophy_state WHERE clan_name = ?", (clan_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_trophies(clan_name: str, trophies: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO trophy_state (clan_name, trophies, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(clan_name) DO UPDATE SET trophies = excluded.trophies,
            updated_at = excluded.updated_at
        """,
        (clan_name, trophies, now),
    )
    conn.commit()
    conn.close()


def log_event(clan_name: str, delta: int, trophies_after: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    event_type = "win" if delta > 0 else "loss"
    cur.execute(
        """
        INSERT INTO trophy_events (clan_name, delta, trophies_after, event_type, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (clan_name, delta, trophies_after, event_type, now),
    )
    conn.commit()
    conn.close()


def get_daily_baseline(clan_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT trophies, level FROM daily_baseline WHERE clan_name = ?", (clan_name,)
    )
    row = cur.fetchone()
    conn.close()
    return row if row else (None, None)


def set_daily_baseline(clan_name: str, trophies: int, level: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO daily_baseline (clan_name, trophies, level, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(clan_name) DO UPDATE SET trophies = excluded.trophies,
            level = excluded.level,
            updated_at = excluded.updated_at
        """,
        (clan_name, trophies, level, now),
    )
    conn.commit()
    conn.close()


def get_daily_baseline_members(clan_name: str) -> set:
    """Roster snapshot taken at the start of the current recap day.
    Used so the daily recap can show everyone who joined/left over the
    whole day, the same way the real-time roster diff works per-poll."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT member_name FROM daily_baseline_members WHERE clan_name = ?",
        (clan_name,),
    )
    rows = cur.fetchall()
    conn.close()
    return {row[0] for row in rows}


def set_daily_baseline_members(clan_name: str, members: list):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_baseline_members WHERE clan_name = ?", (clan_name,))
    cur.executemany(
        "INSERT INTO daily_baseline_members (clan_name, member_name) VALUES (?, ?)",
        [(clan_name, m) for m in members],
    )
    conn.commit()
    conn.close()


def get_clan_rank(clan_name: str):
    """The manually-confirmed leaderboard rank for a clan. This exists
    because JartexNetwork's public API has no endpoint that returns clans
    sorted by trophies/rank - see the /set_clan_rank command."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT rank FROM clan_ranks WHERE clan_name = ?", (clan_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_clan_rank(clan_name: str, rank: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO clan_ranks (clan_name, rank, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(clan_name) DO UPDATE SET rank = excluded.rank,
            updated_at = excluded.updated_at
        """,
        (clan_name, rank, now),
    )
    conn.commit()
    conn.close()


def compute_local_rank(clan_name: str):
    """Auto-computed rank: this clan's position when every *tracked* clan is
    sorted by trophies, descending. There is no JartexNetwork endpoint that
    returns clans sorted by trophies, so this is the only automatic option -
    it's only as accurate as your tracked-clan coverage. If a clan that
    isn't tracked by this bot has more trophies than one that is, this
    number will read higher (better) than the clan's real server-wide rank.
    Track more of the clans above yours (see /track_clan) to close the gap."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT clan_name FROM trophy_state ORDER BY trophies DESC")
    ordered = [r[0].lower() for r in cur.fetchall()]
    conn.close()
    if clan_name.lower() in ordered:
        return ordered.index(clan_name.lower()) + 1
    return None


def get_stored_members(clan_name: str) -> set:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT member_name FROM clan_members WHERE clan_name = ?", (clan_name,)
    )
    rows = cur.fetchall()
    conn.close()
    return {row[0] for row in rows}


def set_stored_members(clan_name: str, members: list):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM clan_members WHERE clan_name = ?", (clan_name,))
    cur.executemany(
        "INSERT INTO clan_members (clan_name, member_name) VALUES (?, ?)",
        [(clan_name, m) for m in members],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# API Interactivity & Leaderboard Resolvers
# ---------------------------------------------------------------------------
def find_key_recursive(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None and d[k] != "":
                return d[k]
        for v in d.values():
            res = find_key_recursive(v, keys)
            if res is not None:
                return res
    elif isinstance(d, list):
        for item in d:
            res = find_key_recursive(item, keys)
            if res is not None:
                return res
    return None


async def fetch_clan_data(session: aiohttp.ClientSession, clan_name: str):
    url = API_URL_TEMPLATE.format(clan_name=clan_name)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 429:
                log.warning(
                    "Rate limited by JartexNetwork API (429) for clan %s.", clan_name
                )
                return None, [], None, None, True
            if resp.status == 404:
                log.warning("Clan '%s' not found on JartexNetwork.", clan_name)
                return None, [], None, None, False
            if resp.status != 200:
                log.warning(
                    "Unexpected status %s fetching clan '%s'", resp.status, clan_name
                )
                return None, [], None, None, False

            data = await resp.json()
            trophies = (
                data.get("currentTrophies")
                or data.get("trophies")
                or data.get("guildTrophies")
                or data.get("clan", {}).get("trophies")
            )
            members_data = (
                data.get("members")
                or data.get("players")
                or data.get("clan", {}).get("members")
                or []
            )
            current_members = []
            # --- THE ROSTER FIX IS RIGHT HERE ---
            if isinstance(members_data, list):
                for m in members_data:
                    if isinstance(m, dict):
                        name = (
                            m.get("name")
                            or m.get("username")
                            or m.get("user", {}).get(
                                "username"
                            )  # <--- Matrixxx user fix
                            or m.get("player", {}).get("name")
                        )
                        if name:
                            current_members.append(str(name))
                    elif isinstance(m, str):
                        current_members.append(m)

            # NOTE: JartexNetwork's public clan API does not currently return
            # a rank/position field (confirmed by inspecting a live response -
            # it only has name/tag/currentTrophies/creationTime/members/owner/
            # leveling). This lookup is kept in case that ever changes, but in
            # practice `rank` will be None here and the real rank comes from
            # the manually-set clan_ranks table (see get_clan_rank / the
            # /set_clan_rank command), since there's no public endpoint that
            # returns clans sorted by trophy count.
            rank = find_key_recursive(
                data,
                [
                    "rank",
                    "position",
                    "leaderboardRank",
                    "clanRank",
                    "placement",
                    "leaderboard_rank",
                    "rankPosition",
                ],
            )
            level = find_key_recursive(
                data, ["level", "clanLevel", "clan_level", "guildLevel"]
            )
            return trophies, current_members, rank, level, False
    except Exception as e:
        log.error("Error fetching clan '%s': %s", clan_name, e)
        return None, [], None, None, False


# ---------------------------------------------------------------------------
# Bot Infrastructure & Loop Scheduling
# ---------------------------------------------------------------------------
# The fix is here! We use Intents.all() and removed the duplicate overwriting line.
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

_CHANNEL_CHECK_EXEMPT_COMMANDS = {"set_command_channel", "clear_command_channel"}


async def _command_channel_check(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    if (
        interaction.command
        and interaction.command.name in _CHANNEL_CHECK_EXEMPT_COMMANDS
    ):
        return True
    configured_channel_id = get_command_channel(str(interaction.guild.id))
    if configured_channel_id is None:
        return True
    if str(interaction.channel_id) != configured_channel_id:
        await interaction.response.send_message(
            f"Commands for this bot can only be used in <#{configured_channel_id}>.",
            ephemeral=True,
        )
        return False
    return True


bot.tree.interaction_check = _command_channel_check


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need the **Manage Server** permission to use this command."
    elif isinstance(error, app_commands.CheckFailure):
        return
    else:
        log.error("Unhandled app command error: %s", error)
        msg = "Something went wrong running that command."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_trophies():
    global _current_backoff
    clans = get_tracked_clans()
    if not clans:
        return

    default_channel = (
        bot.get_channel(int(ANNOUNCE_CHANNEL_ID)) if ANNOUNCE_CHANNEL_ID else None
    )

    async with aiohttp.ClientSession() as session:
        for i, (clan_name, channel_id) in enumerate(clans):
            (
                current_trophies,
                current_members,
                rank,
                level,
                rate_limited,
            ) = await fetch_clan_data(session, clan_name)

            if rate_limited:
                await asyncio.sleep(_current_backoff)
                _current_backoff = min(_current_backoff * 2, BACKOFF_MAX_SECONDS)
                continue
            _current_backoff = BACKOFF_INITIAL_SECONDS

            if current_trophies is not None:
                target_channel = (
                    bot.get_channel(int(channel_id)) if channel_id else default_channel
                )
                if channel_id and not target_channel:
                    target_channel = default_channel

                await _handle_clan_result(
                    clan_name,
                    current_trophies,
                    current_members,
                    rank,
                    level,
                    target_channel,
                )

            if i < len(clans) - 1:
                await asyncio.sleep(PER_CLAN_REQUEST_DELAY_SECONDS)


def _format_member_list(members, char_limit: int = 950) -> str:
    """Turn a set/list of member names into a clean, comma-separated string
    for an embed field. Discord caps embed field values at 1024 characters,
    so for big clans/rosters this trims the list and appends a '+N more'
    note instead of overflowing or getting cut off mid-name."""
    names = sorted(members)
    joined = ", ".join(names)
    if len(joined) <= char_limit:
        return joined

    kept = []
    running_len = 0
    for name in names:
        addition = len(name) + 2  # account for the ", " separator
        if running_len + addition > char_limit:
            break
        kept.append(name)
        running_len += addition

    remaining = len(names) - len(kept)
    return f"{', '.join(kept)}, *+{remaining} more*"


@tasks.loop(time=time(hour=0, minute=0, second=0, tzinfo=timezone.utc))
async def daily_recap_loop():
    clans = get_tracked_clans()
    if not clans:
        return

    default_channel = (
        bot.get_channel(int(ANNOUNCE_CHANNEL_ID)) if ANNOUNCE_CHANNEL_ID else None
    )

    async with aiohttp.ClientSession() as session:
        for i, (clan_name, channel_id) in enumerate(clans):
            (
                current_trophies,
                current_members,
                rank,
                level,
                rate_limited,
            ) = await fetch_clan_data(session, clan_name)

            if rate_limited or current_trophies is None:
                continue

            target_channel = (
                bot.get_channel(int(channel_id)) if channel_id else default_channel
            )
            if not target_channel:
                target_channel = default_channel

            if target_channel:
                baseline_trophies, baseline_level = get_daily_baseline(clan_name)
                # Roster as it looked at the start of this recap day, so we
                # can report everyone who joined/left over the full day -
                # same idea as the trophy baseline, just for the member list.
                baseline_members = get_daily_baseline_members(clan_name)

                if baseline_trophies is None:
                    set_daily_baseline(clan_name, current_trophies, level)
                    set_daily_baseline_members(clan_name, current_members)
                    continue

                daily_delta = current_trophies - baseline_trophies
                delta_sign = "+" if daily_delta >= 0 else ""

                # Diff today's roster against the snapshot taken at the
                # start of the day to find who joined and who left.
                current_members_set = set(current_members)
                joined_today = current_members_set - baseline_members
                left_today = baseline_members - current_members_set

                if rank is None or rank == "":
                    # JartexNetwork's API doesn't expose the real global rank.
                    # Prefer a manually-confirmed rank (/set_clan_rank) if one
                    # was ever set; otherwise auto-compute from trophies of
                    # the clans this bot tracks. The auto value is only as
                    # accurate as your tracked-clan coverage - see
                    # compute_local_rank()'s docstring.
                    rank = get_clan_rank(clan_name) or compute_local_rank(clan_name)

                rank_prefix = f"[#{rank}] " if rank else ""
                date_str = datetime.now(timezone.utc).strftime("%d %B %Y")

                # --- Build a proper embed instead of a plain-text wall,
                # matching the look of the real-time poll_trophies embeds. ---
                if daily_delta > 0:
                    trend_emoji = "\U0001f4c8"
                    embed_color = discord.Color.brand_green()
                elif daily_delta < 0:
                    trend_emoji = "\U0001f4c9"
                    embed_color = discord.Color.red()
                else:
                    trend_emoji = "\u2796"
                    embed_color = discord.Color.blurple()

                embed = discord.Embed(
                    color=embed_color,
                    description=f"**{date_str}**",
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_author(
                    name=f"{rank_prefix}{clan_name} • {trend_emoji} Daily Recap"
                )
                embed.add_field(
                    name="\U0001f3c6 Trophies",
                    value=(
                        f"`{baseline_trophies:,}` → `{current_trophies:,}`\n"
                        f"**Change:** `{delta_sign}{daily_delta:,}`"
                    ),
                    inline=True,
                )
                if baseline_level is not None and level is not None:
                    level_value = (
                        f"`{baseline_level}` → `{level}`"
                        if baseline_level != level
                        else f"`{level}` *(no change)*"
                    )
                    embed.add_field(name="\u2b50 Level", value=level_value, inline=True)

                # Show just the count of members joined/left instead of listing names
                joined_count = len(joined_today)
                embed.add_field(
                    name=f"\U0001f4e5 Members Joined",
                    value=f"**{joined_count}**" if joined_count > 0 else "*None*",
                    inline=False,
                )

                left_count = len(left_today)
                embed.add_field(
                    name=f"\U0001f4e4 Members Left",
                    value=f"**{left_count}**" if left_count > 0 else "*None*",
                    inline=False,
                )

                embed.set_footer(text="JartexNetwork • Daily Recap")
                await target_channel.send(embed=embed)

                set_daily_baseline(clan_name, current_trophies, level)
                set_daily_baseline_members(clan_name, current_members)

            if i < len(clans) - 1:
                await asyncio.sleep(PER_CLAN_REQUEST_DELAY_SECONDS)


async def _handle_clan_result(
    clan_name: str, current: int, current_members: list, rank, level, channel
):
    last = get_last_trophies(clan_name)
    old_members = get_stored_members(clan_name)

    if last is None:
        set_trophies(clan_name, current)
        set_stored_members(clan_name, current_members)
        b_trophies, _ = get_daily_baseline(clan_name)
        if b_trophies is None:
            set_daily_baseline(clan_name, current, level)
            log.info("Baseline data set for %s: %s trophies", clan_name, current)
        return

    current_members_set = set(current_members)
    joined_members = current_members_set - old_members
    left_members = old_members - current_members_set

    trophy_changed = current != last
    member_changed = len(joined_members) > 0 or len(left_members) > 0

    if not trophy_changed and not member_changed:
        return

    delta = current - last

    if trophy_changed:
        set_trophies(clan_name, current)
        log_event(clan_name, delta, current)

    if member_changed:
        set_stored_members(clan_name, current_members)

    if rank is None or rank == "":
        # Same reasoning as daily_recap_loop: prefer a manual override
        # (/set_clan_rank) if set, otherwise auto-compute from the trophies
        # of tracked clans.
        rank = get_clan_rank(clan_name) or compute_local_rank(clan_name)

    rank_str = f"[#{rank}] " if rank else ""

    log.info(
        "%sTrophy change for %s: %+d (now %s), RosterChange=%s",
        rank_str,
        clan_name,
        delta,
        current,
        member_changed,
    )

    if channel:
        is_gain = delta > 0
        trend_emoji = (
            "\U0001f4c8" if is_gain else ("\U0001f4c9" if delta < 0 else "\U0001f465")
        )
        verb = "Gain" if is_gain else ("Loss" if delta < 0 else "Roster Update")
        change_str = f"+{delta:,}" if is_gain else f"{delta:,}"
        ansi_color = (
            "\u001b[32m" if is_gain else ("\u001b[31m" if delta < 0 else "\u001b[37m")
        )
        embed_color = (
            discord.Color.brand_green()
            if is_gain
            else (discord.Color.red() if delta < 0 else discord.Color.blue())
        )

        embed = discord.Embed(
            color=embed_color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=f"{rank_str}{clan_name} • {trend_emoji} {verb}")

        # Left Side: Compact layout with rocket progression and colon symbol
        embed.add_field(
            name="\U0001f3c6 Current Trophies:",
            value=f"\u200b \u200b \u200b`{current:,}`\n"
                  f"🚀 **Progression:**\n"
                  f"`{last:,}` → `{current:,}`",
            inline=True,
        )
        # Right Side: Isolated change bar block only
        embed.add_field(
            name="\U0001f504 Change",
            value=f"```ansi\n{ansi_color}{change_str}\u001b[0m\n```",
            inline=True,
        )

        if member_changed:
            member_activity = []
            if joined_members:
                member_activity.append(
                    f"\U0001f4e5 **Joined:** {', '.join(joined_members)}"
                )
            if left_members:
                member_activity.append(
                    f"\U0001f4e4 **Left:** {', '.join(left_members)}"
                )
            embed.add_field(
                name="\U0001f465 Roster Shifts",
                value="\n".join(member_activity),
                inline=False,
            )

        embed.set_footer(text="JartexNetwork • Auto Tracker")
        await channel.send(embed=embed)


@poll_trophies.before_loop
async def before_poll():
    await bot.wait_until_ready()


@daily_recap_loop.before_loop
async def before_daily():
    await bot.wait_until_ready()


async def _leave_if_not_allowed(guild: discord.Guild) -> bool:
    if not ALLOWED_GUILD_IDS:
        return False
    if str(guild.id) in ALLOWED_GUILD_IDS:
        return False
    log.warning("Leaving unauthorized server '%s' (ID: %s)", guild.name, guild.id)
    try:
        target = guild.system_channel
        if target is None:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target = ch
                    break
        if target:
            await target.send(
                "This bot is private and hasn't been authorized for this server."
            )
    except Exception:
        pass
    await guild.leave()
    return True


@bot.event
async def on_guild_join(guild: discord.Guild):
    await _leave_if_not_allowed(guild)


@bot.event
async def on_ready():
    init_db()
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    if ALLOWED_GUILD_IDS:
        for guild in list(bot.guilds):
            left = await _leave_if_not_allowed(guild)
            if not left:
                log.info("Authorized server: %s (ID: %s)", guild.name, guild.id)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))
    except Exception as e:
        log.error("Failed to sync slash commands: %s", e)

    if not poll_trophies.is_running():
        poll_trophies.start()
    if not daily_recap_loop.is_running():
        daily_recap_loop.start()


def _is_bot_owner(interaction: discord.Interaction) -> bool:
    if not BOT_OWNER_IDS:
        return True
    return str(interaction.user.id) in BOT_OWNER_IDS


@bot.tree.command(
    name="set_command_channel",
    description="Restrict this bot's commands to the channel this is run in (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_command_channel_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works inside a server.", ephemeral=True
        )
        return
    if not _is_bot_owner(interaction):
        await interaction.response.send_message(
            "Only the bot's designated owner(s) can change this setting.",
            ephemeral=True,
        )
        return

    set_command_channel(str(interaction.guild.id), str(interaction.channel_id))
    await interaction.response.send_message(
        f"Done - from now on, this bot's commands can only be used in {interaction.channel.mention}."
    )


@bot.tree.command(
    name="clear_command_channel",
    description="Remove the channel restriction, allowing commands anywhere again (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def clear_command_channel_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works inside a server.", ephemeral=True
        )
        return
    if not _is_bot_owner(interaction):
        await interaction.response.send_message(
            "Only the bot's designated owner(s) can change this setting.",
            ephemeral=True,
        )
        return

    removed = clear_command_channel(str(interaction.guild.id))
    if removed:
        await interaction.response.send_message(
            "Channel restriction removed - commands can now be used anywhere in this server."
        )
    else:
        await interaction.response.send_message("No channel restriction was set.")


@bot.tree.command(
    name="track_clan", description="Start tracking trophy changes for a clan."
)
@app_commands.describe(
    clan_name="Exact clan name/tag as it appears on JartexNetwork",
    channel="Channel for this clan's updates (defaults to the bot's default announce channel if omitted)",
)
async def track_clan(
    interaction: discord.Interaction,
    clan_name: str,
    channel: discord.TextChannel = None,
):
    channel_id = str(channel.id) if channel else None
    added = add_tracked_clan(clan_name, channel_id)
    where = f" in {channel.mention}" if channel else " in the default announce channel"

    if added:
        await interaction.response.send_message(
            f"Now tracking **{clan_name}**{where}. Its first poll will set a baseline."
        )
    elif channel_id:
        await interaction.response.send_message(
            f"**{clan_name}** was already tracked - updated its channel to {channel.mention}."
        )
    else:
        await interaction.response.send_message(f"**{clan_name}** is already being tracked.")


@bot.tree.command(
    name="untrack_clan", description="Stop tracking trophy changes for a clan."
)
@app_commands.describe(clan_name="Exact clan name/tag as currently tracked")
async def untrack_clan(interaction: discord.Interaction, clan_name: str):
    removed = remove_tracked_clan(clan_name)
    if removed:
        await interaction.response.send_message(f"Stopped tracking **{clan_name}**.")
    else:
        await interaction.response.send_message(f"**{clan_name}** wasn't being tracked.")


@bot.tree.command(
    name="set_clan_rank",
    description="Manually set a clan's real leaderboard rank (JartexNetwork's API doesn't expose this).",
)
@app_commands.describe(
    clan_name="Exact clan name/tag as currently tracked",
    rank="The clan's current global rank - check in-game via /c info",
)
async def set_clan_rank_cmd(
    interaction: discord.Interaction, clan_name: str, rank: int
):
    if rank < 1:
        await interaction.response.send_message(
            "Rank has to be a positive number.", ephemeral=True
        )
        return

    set_clan_rank(clan_name, rank)
    await interaction.response.send_message(
        f"Set **{clan_name}**'s rank to `#{rank}`. This will show in trophy "
        f"updates and daily recaps until you update it again - JartexNetwork "
        f"doesn't publish rank through the stats API, so it won't auto-correct "
        f"on its own."
    )


@bot.tree.command(
    name="list_clans", description="List all clans currently being tracked."
)
async def list_clans(interaction: discord.Interaction):
    clans = get_tracked_clans()
    if not clans:
        await interaction.response.send_message(
            "No clans are being tracked yet. Use `/track_clan` to add one."
        )
        return

    lines = []
    for clan_name, channel_id in clans:
        if channel_id:
            lines.append(f"- **{clan_name}** → <#{channel_id}>")
        else:
            lines.append(f"- **{clan_name}** → default announce channel")

    await interaction.response.send_message("Tracked clans:\n" + "\n".join(lines))


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    bot.run(DISCORD_TOKEN)