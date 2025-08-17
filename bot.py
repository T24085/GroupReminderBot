# bot.py (fixed)
import os
import sys
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, ButtonStyle, Interaction
from discord.ext import commands
from discord.ui import View, button
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

import dateparser
import pytz
from dotenv import load_dotenv

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]"
)
log = logging.getLogger(__name__)

# ---------- Token / Env ----------
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

def token_looks_valid(t: str) -> bool:
    return bool(t) and t.count(".") == 2 and not t.startswith("mfa.")

if not token_looks_valid(TOKEN):
    sys.exit(
        "DISCORD_BOT_TOKEN missing or malformed.\n"
        "• Put it in .env as: DISCORD_BOT_TOKEN=AAA.BBB.CCC (no quotes)\n"
        f"• Loaded .env from: {env_path}"
    )

# ---------- Intents / Bot ----------
INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.message_content = False

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
scheduler = AsyncIOScheduler()

DB_PATH = "remindbot.db"

# ---------- DB Helpers ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_column(conn, table: str, column: str, ddl: str):
    """Add a column if it doesn't exist.
    ddl should be the SQL *type/constraints* (e.g. "TEXT"), column name is provided separately.
    """
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table});")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
        conn.commit()


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            tz TEXT DEFAULT 'America/Chicago'
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            when_utc TEXT,
            cron TEXT,
            mention_role_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER,
            user_id INTEGER,
            message TEXT NOT NULL,
            when_utc TEXT NOT NULL,
            mention_role_id INTEGER,
            created_at TEXT NOT NULL
        );
        """
    )

    # Forward-compat columns
    ensure_column(conn, "events", "lead_minutes", "TEXT")
    ensure_column(conn, "events", "message_id", "INTEGER")

    # RSVP storage
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS event_rsvps (
            event_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            status   TEXT NOT NULL, -- "going" | "not" | "maybe"
            updated_at TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id),
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        """
    )

    conn.commit()
    conn.close()


# ---------- Time Helpers ----------
def parse_human_time(human_text: str, tz_name: str) -> Optional[datetime]:
    settings = {
        "TIMEZONE": tz_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    dt = dateparser.parse(human_text, settings=settings)
    if not dt:
        return None
    return dt.astimezone(timezone.utc)


async def user_timezone(user_id: int) -> str:
    conn = db()
    cur = conn.cursor()
    # Correct parameterized query
    cur.execute("SELECT tz FROM user_prefs WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "America/Chicago"


def fmt_when_for_user(dt_utc: datetime, tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    local = dt_utc.astimezone(tz)
    return local.strftime("%Y-%m-%d %I:%M %p %Z")


# ---------- Message Senders ----------
async def send_event(guild_id: int, channel_id: int, title: str, mention_role_id: Optional[int]):
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    channel = guild.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    mention = f" <@&{mention_role_id}>" if mention_role_id else ""
    await channel.send(f"⏰ **Event Reminder:** {title}{mention}")


async def send_reminder(reminder_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, guild_id, channel_id, user_id, message, when_utc, mention_role_id
               FROM reminders WHERE id=?""",
        (reminder_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return

    _id, guild_id, channel_id, user_id, message, when_utc, mention_role_id = row
    content = f"🔔 **Reminder:** {message}"
    if mention_role_id:
        content += f" <@&{mention_role_id}>"

    if channel_id:
        guild = bot.get_guild(guild_id) if guild_id else None
        if guild:
            channel = guild.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                await channel.send(content)
    else:
        user = await bot.fetch_user(user_id)
        if user:
            await user.send(content)

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()


# ---------- RSVP Helpers & UI ----------
async def compute_rsvp_counts(event_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, GROUP_CONCAT(user_id)
        FROM event_rsvps
        WHERE event_id=?
        GROUP BY status
        """,
        (event_id,),
    )
    rows = cur.fetchall()
    conn.close()
    counts = {"going": [], "maybe": [], "not": []}
    for status, ids in rows:
        if not ids:
            continue
        counts[status] = [int(x) for x in ids.split(",")]
    return counts


async def format_rsvp_lines(counts: dict) -> str:
    g, m, n = (counts.get("going", []), counts.get("maybe", []), counts.get("not", []))
    def mention_list(lst):
        return ", ".join(f"<@{u}>" for u in lst) if lst else "—"
    lines = [
        f"**✅ Going ({len(g)}):** {mention_list(g)}",
        f"**❓ Maybe ({len(m)}):** {mention_list(m)}",
        f"**❌ Not Going ({len(n)}):** {mention_list(n)}",
    ]
    return "\n".join(lines)


# --- RSVP Helpers ---
async def update_announcement_message(event_id: int, message: discord.Message):
    """Update an existing RSVP embed message with the latest RSVP counts."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT title, when_utc, mention_role_id FROM events WHERE id=?", (event_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return
    title, when_utc, role_id = row
    counts = await get_rsvp_counts(event_id)

    creator_tz = await user_timezone(message.author.id)
    when_txt = "recurring" if not when_utc else fmt_when_for_user(datetime.fromisoformat(when_utc), creator_tz)
    mention = f"<@&{role_id}>" if role_id else ""
    rsvp_block = await format_rsvp_lines(counts)

    embed = discord.Embed(
        title=f"📅 {title}",
        description=f"**When:** {when_txt}\n{mention}\n\n{rsvp_block}",
        color=0x5b8cfa
    )
    await message.edit(embed=embed, view=RSVPView(event_id=event_id))


# --- NEW: Event Announcer ---
async def announce_event(event_id: int, channel: discord.TextChannel, requester_user_id: int):
    """Post the RSVP embed with buttons for an event and remember message_id."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT title, when_utc, mention_role_id FROM events WHERE id=?", (event_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return
    title, when_utc, role_id = row

    # Build embed
    creator_tz = await user_timezone(requester_user_id)
    when_txt = "recurring" if not when_utc else fmt_when_for_user(datetime.fromisoformat(when_utc), creator_tz)
    mention = f"<@&{role_id}>" if role_id else ""
    counts = {"going": [], "maybe": [], "not": []}
    rsvp_block = await format_rsvp_lines(counts)

    embed = discord.Embed(
        title=f"📅 {title}",
        description=f"**When:** {when_txt}\n{mention}\n\n{rsvp_block}",
        color=0x5b8cfa
    )
    view = RSVPView(event_id=event_id)
    msg = await channel.send(embed=embed, view=view)

    # Store message_id for live updates
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE events SET message_id=? WHERE id=?", (msg.id, event_id))
    conn.commit()
    conn.close()


# --- Slash Command: Create Event ---
@tree.command(name="event_create", description="Create a new event with title, time, and optional role mention")
@app_commands.describe(title="Title of the event", when="When the event will happen (in your timezone)", role="Role to mention")
async def create(interaction: discord.Interaction, title: str, when: str, role: discord.Role = None):
    ...


    counts = await compute_rsvp_counts(event_id)
    rsvp_block = await format_rsvp_lines(counts)
    when_txt = "recurring" if not when_utc else fmt_when_for_user(
        datetime.fromisoformat(when_utc), "America/Chicago"
    )
    mention = f"<@&{role_id}>" if role_id else ""

    embed = discord.Embed(
        title=f"📅 {title}",
        description=f"**When:** {when_txt}\n{mention}\n\n{rsvp_block}",
        color=0x5b8cfa,
    )
    try:
        await msg.edit(embed=embed)
    except Exception:
        pass


class RSVPView(View):
    def __init__(self, event_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.event_id = event_id

    async def _set_status(self, interaction: Interaction, status: str):
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO event_rsvps (event_id, user_id, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, user_id)
            DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
            """,
            (self.event_id, interaction.user.id, status, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        await update_announcement_message(self.event_id)
        await interaction.response.send_message(f"✅ RSVP updated: **{status.upper()}**", ephemeral=True)

    @button(label="✅ Going", style=ButtonStyle.success, custom_id="rsvp_going")
    async def going(self, interaction: Interaction, _: discord.ui.Button):
        await self._set_status(interaction, "going")

    @button(label="❌ Not Going", style=ButtonStyle.danger, custom_id="rsvp_not")
    async def notgoing(self, interaction: Interaction, _: discord.ui.Button):
        await self._set_status(interaction, "not")

    @button(label="❓ Maybe", style=ButtonStyle.secondary, custom_id="rsvp_maybe")
    async def maybe(self, interaction: Interaction, _: discord.ui.Button):
        await self._set_status(interaction, "maybe")


# ---------- Startup Rescheduling ----------
def schedule_loaded_jobs():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """SELECT id, guild_id, channel_id, title, when_utc, mention_role_id, lead_minutes
               FROM events WHERE when_utc IS NOT NULL"""
    )
    for ev_id, guild_id, channel_id, title, when_utc, mention_role_id, lead_str in cur.fetchall():
        run_dt = datetime.fromisoformat(when_utc)
        if run_dt > datetime.now(timezone.utc):
            scheduler.add_job(
                lambda g=guild_id, c=channel_id, t=title, r=mention_role_id: asyncio.create_task(send_event(g, c, t, r)),
                trigger=DateTrigger(run_date=run_dt),
                id=f"event:{ev_id}",
                replace_existing=True,
            )
        if lead_str:
            try:
                mins_list = sorted({int(x.strip()) for x in lead_str.split(',') if x.strip()}, reverse=True)
                mins_list = [m for m in mins_list if m > 0]
            except Exception:
                mins_list = []
            for m in mins_list:
                lead_dt = run_dt - timedelta(minutes=m)
                if lead_dt > datetime.now(timezone.utc):
                    scheduler.add_job(
                        lambda g=guild_id, c=channel_id, t=title, r=mention_role_id, mins=m:
                            asyncio.create_task(send_event(g, c, f"{t} starts in {mins} min", r)),
                        trigger=DateTrigger(run_date=lead_dt),
                        id=f"event:{ev_id}:lead:{m}",
                        replace_existing=True,
                    )

    cur.execute(
        """SELECT id, guild_id, channel_id, title, cron, mention_role_id FROM events WHERE cron IS NOT NULL"""
    )
    for ev_id, guild_id, channel_id, title, cron_expr, mention_role_id in cur.fetchall():
        try:
            minute, hour, dom, month, dow = cron_expr.split()
            scheduler.add_job(
                lambda g=guild_id, c=channel_id, t=title, r=mention_role_id: asyncio.create_task(send_event(g, c, t, r)),
                trigger=CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow),
                id=f"event:{ev_id}",
                replace_existing=True,
            )
        except Exception as e:
            log.warning(f"Bad cron for event {ev_id}: {e}")

    cur.execute("SELECT id, when_utc FROM reminders")
    for rem_id, when_utc in cur.fetchall():
        when_dt = datetime.fromisoformat(when_utc)
        if when_dt > datetime.now(timezone.utc):
            scheduler.add_job(
                lambda r=rem_id: asyncio.create_task(send_reminder(r)),
                trigger=DateTrigger(run_date=when_dt),
                id=f"reminder:{rem_id}",
                replace_existing=True,
            )

    conn.close()


# ---------- Commands ----------
@tree.command(name="timezone_set", description="Set your timezone, e.g. America/Chicago or Europe/London")
@app_commands.describe(tz="IANA timezone like America/Chicago, Europe/London, Asia/Tokyo")
async def timezone_set(interaction: discord.Interaction, tz: str):
    try:
        pytz.timezone(tz)
    except Exception:
        await interaction.response.send_message(
            "❌ Unknown timezone. Try `America/Chicago`, `Europe/London`, `Asia/Tokyo`.",
            ephemeral=True,
        )
        return
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_prefs(user_id, tz) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
        (interaction.user.id, tz),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Timezone set to **{tz}**.", ephemeral=True)


event_group = app_commands.Group(name="event", description="Create and manage group events")
tree.add_command(event_group)


@event_group.command(name="create", description="Create a one-off event or recurring cron event")
@app_commands.describe(
    title="Title of the event",
    when="When (e.g., 'next Tue 7pm', '2025-08-20 19:00', 'in 45 minutes'). Leave blank if using cron.",
    channel="Channel to announce in (defaults to current channel)",
    mention_role="Role to mention (optional)",
    cron="Optional cron: 'min hour dom month dow' (e.g., '0 19 * * TUE' for Tue 7pm)",
    lead_minutes="Comma-separated minutes before start (e.g., '60,10')",
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    when: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
    mention_role: Optional[discord.Role] = None,
    cron: Optional[str] = None,
    lead_minutes: Optional[str] = None,
):
    if not channel:
        channel = interaction.channel  # type: ignore
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Pick a text channel for announcements.", ephemeral=True)
        return

    if not when and not cron:
        await interaction.response.send_message("❌ Provide either `when` or `cron`.", ephemeral=True)
        return

    when_utc_iso = None
    if when:
        tz = await user_timezone(interaction.user.id)
        dt_utc = parse_human_time(when, tz)
        if not dt_utc:
            await interaction.response.send_message(
                "❌ Couldn't parse the time. Try 'next Tue 7pm' or '2025-08-20 19:00'.",
                ephemeral=True,
            )
            return
        when_utc_iso = dt_utc.isoformat()

    lead_list = []
    if lead_minutes:
        try:
            lead_list = sorted({int(x.strip()) for x in lead_minutes.split(',') if x.strip()}, reverse=True)
            lead_list = [m for m in lead_list if m > 0]
        except Exception:
            await interaction.response.send_message("❌ Bad `lead_minutes`. Example: `60,10`", ephemeral=True)
            return

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO events (guild_id, channel_id, title, when_utc, cron, mention_role_id, created_by, created_at, lead_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interaction.guild_id,
            channel.id,
            title,
            when_utc_iso,
            cron,
            mention_role.id if mention_role else None,
            interaction.user.id,
            datetime.now(timezone.utc).isoformat(),
            ','.join(map(str, lead_list)) if lead_list else None,
        ),
    )
    ev_id = cur.lastrowid
    conn.commit()
    conn.close()

    if when_utc_iso:
        run_dt = datetime.fromisoformat(when_utc_iso)
        scheduler.add_job(
            lambda g=interaction.guild_id, c=channel.id, t=title, r=(mention_role.id if mention_role else None): asyncio.create_task(send_event(g, c, t, r)),
            trigger=DateTrigger(run_date=run_dt),
            id=f"event:{ev_id}",
            replace_existing=True,
        )
        for m in lead_list:
            lead_dt = run_dt - timedelta(minutes=m)
            if lead_dt > datetime.now(timezone.utc):
                scheduler.add_job(
                    lambda g=interaction.guild_id, c=channel.id, t=title, r=(mention_role.id if mention_role else None), mins=m: asyncio.create_task(send_event(g, c, f"{t} starts in {mins} min", r)),
                    trigger=DateTrigger(run_date=lead_dt),
                    id=f"event:{ev_id}:lead:{m}",
                    replace_existing=True,
                )

        tz_name = await user_timezone(interaction.user.id)
        pretty = fmt_when_for_user(run_dt, tz_name)
        extras = f" (lead: {','.join(map(str, lead_list))} min)" if lead_list else ""
        # Post RSVP buttons automatically
        await announce_event(ev_id, channel, interaction.user.id)

        await interaction.response.send_message(
            f"✅ Event created **and announced**: **{title}** — fires **{pretty}** in {channel.mention}{extras}",
            ephemeral=False
        )
        return

    try:
        minute, hour, dom, month, dow = cron.split()
        scheduler.add_job(
            lambda g=interaction.guild_id, c=channel.id, t=title, r=(mention_role.id if mention_role else None): asyncio.create_task(send_event(g, c, t, r)),
            trigger=CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow),
            id=f"event:{ev_id}",
            replace_existing=True,
        )
        lead_note = " (lead minutes not used with cron yet)" if lead_list else ""
        await interaction.response.send_message(
            f"✅ Recurring event created: **{title}** — cron `{cron}` → {channel.mention}{lead_note}",
            ephemeral=False,
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Bad cron expression. Example: `0 19 * * TUE` for Tuesdays at 7pm. ({e})",
            ephemeral=True,
        )


@event_group.command(name="list", description="List upcoming or recurring events")
async def event_list(interaction: discord.Interaction):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, when_utc, cron, channel_id, mention_role_id, lead_minutes
        FROM events
        WHERE guild_id=?
        ORDER BY CASE WHEN when_utc IS NULL THEN 1 ELSE 0 END, when_utc ASC
        LIMIT 20
        """,
        (interaction.guild_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📭 No events yet. Try `/event create`.", ephemeral=True)
        return

    user_tz = await user_timezone(interaction.user.id)
    lines = []
    for ev_id, title, when_utc, cron, channel_id, role_id, lead_str in rows:
        where = f"in <#{channel_id}>"
        role_txt = f" (mentions <@&{role_id}>)" if role_id else ""
        lead_txt = f" [lead: {lead_str}]" if lead_str else ""
        if when_utc:
            dt_utc = datetime.fromisoformat(when_utc)
            pretty = fmt_when_for_user(dt_utc, user_tz)
            lines.append(f"• **[{ev_id}]** {title} — **{pretty}** {where}{role_txt}{lead_txt}")
        else:
            lines.append(f"• **[{ev_id}]** {title} — cron `{cron}` {where}{role_txt}{lead_txt}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@event_group.command(name="delete", description="Delete an event by its ID (see /event list)")
@app_commands.describe(event_id="The numeric ID shown in /event list")
async def event_delete(interaction: discord.Interaction, event_id: int):
    try:
        scheduler.remove_job(f"event:{event_id}")
    except Exception:
        pass

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT lead_minutes FROM events WHERE id=? AND guild_id=?", (event_id, interaction.guild_id))
    row = cur.fetchone()
    if row and row[0]:
        try:
            for m in {int(x.strip()) for x in row[0].split(',') if x.strip()}:
                try:
                    scheduler.remove_job(f"event:{event_id}:lead:{m}")
                except Exception:
                    pass
        except Exception:
            pass

    cur.execute("DELETE FROM events WHERE id=? AND guild_id=?", (event_id, interaction.guild_id))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await interaction.response.send_message(f"🗑️ Deleted event **{event_id}**.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Event not found.", ephemeral=True)


# ---------- RSVP Commands ----------
@event_group.command(name="announce", description="Post an RSVP message with buttons for an existing event")
@app_commands.describe(event_id="ID from /event list", channel="Channel to post in (defaults to here)")
async def event_announce(interaction: discord.Interaction, event_id: int, channel: Optional[discord.TextChannel] = None):
    if not channel:
        channel = interaction.channel  # type: ignore
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Pick a text channel.", ephemeral=True)
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT title, when_utc, mention_role_id FROM events WHERE id=? AND guild_id=?", (event_id, interaction.guild_id))
    row = cur.fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        return

    title, when_utc, role_id = row
    creator_tz = await user_timezone(interaction.user.id)
    when_txt = "recurring" if not when_utc else fmt_when_for_user(datetime.fromisoformat(when_utc), creator_tz)
    mention = f"<@&{role_id}>" if role_id else ""
    counts = {"going": [], "maybe": [], "not": []}
    rsvp_block = await format_rsvp_lines(counts)

    embed = discord.Embed(title=f"📅 {title}", description=f"**When:** {when_txt}\n{mention}\n\n{rsvp_block}", color=0x5b8cfa)
    view = RSVPView(event_id=event_id)
    msg = await channel.send(embed=embed, view=view)

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE events SET message_id=? WHERE id=?", (msg.id, event_id))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"✅ Announced in {channel.mention}", ephemeral=True)


@event_group.command(name="rsvps", description="Show RSVP counts and lists for an event")
@app_commands.describe(event_id="ID from /event list")
async def event_rsvps(interaction: discord.Interaction, event_id: int):
    counts = await compute_rsvp_counts(event_id)
    if not any(counts.values()):
        await interaction.response.send_message("No RSVPs yet.", ephemeral=True)
        return
    lines = await format_rsvp_lines(counts)
    await interaction.response.send_message(lines, ephemeral=False)


remind_group = app_commands.Group(name="remind", description="One-off reminders")
tree.add_command(remind_group)


@remind_group.command(name="add", description="Create a reminder (DM you or post in a channel)")
@app_commands.describe(
    when="e.g., 'in 30 minutes', 'tomorrow 9am', '2025-08-20 19:00'",
    message="What to remind",
    channel="Optional channel to post in (leave blank to DM you)",
    mention_role="Optional role to mention (channel reminders only)",
)
async def remind_add(
    interaction: discord.Interaction,
    when: str,
    message: str,
    channel: Optional[discord.TextChannel] = None,
    mention_role: Optional[discord.Role] = None,
):
    tz = await user_timezone(interaction.user.id)
    dt_utc = parse_human_time(when, tz)
    if not dt_utc:
        await interaction.response.send_message("❌ Couldn't parse the time. Try 'in 30 minutes' or 'tomorrow 9am'.", ephemeral=True)
        return

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reminders (guild_id, channel_id, user_id, message, when_utc, mention_role_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interaction.guild_id,
            channel.id if channel else None,
            interaction.user.id,
            message,
            dt_utc.isoformat(),
            mention_role.id if mention_role else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    rem_id = cur.lastrowid
    conn.commit()
    conn.close()

    scheduler.add_job(
        lambda r=rem_id: asyncio.create_task(send_reminder(r)),
        trigger=DateTrigger(run_date=dt_utc),
        id=f"reminder:{rem_id}",
        replace_existing=True,
    )

    pretty = fmt_when_for_user(dt_utc, tz)
    loc = channel.mention if channel else "your DMs"
    await interaction.response.send_message(f"✅ Reminder set for **{pretty}** → {loc}", ephemeral=True)


@remind_group.command(name="list", description="List your upcoming reminders")
async def remind_list(interaction: discord.Interaction):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, message, when_utc, channel_id, mention_role_id
        FROM reminders
        WHERE user_id=?
        ORDER BY when_utc ASC
        LIMIT 20
        """,
        (interaction.user.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📭 You have no reminders.", ephemeral=True)
        return

    tz = await user_timezone(interaction.user.id)
    lines = []
    for rem_id, message, when_utc, channel_id, role_id in rows:
        dt_utc = datetime.fromisoformat(when_utc)
        pretty = fmt_when_for_user(dt_utc, tz)
        where = f"in <#{channel_id}>" if channel_id else "in DM"
        role_txt = f" (mentions <@&{role_id}>)" if role_id else ""
        lines.append(f"• **[{rem_id}]** {pretty} — {where}{role_txt} — “{message}”")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@remind_group.command(name="delete", description="Delete a reminder by ID (see /remind list)")
@app_commands.describe(reminder_id="The numeric ID from /remind list")
async def remind_delete(interaction: discord.Interaction, reminder_id: int):
    try:
        scheduler.remove_job(f"reminder:{reminder_id}")
    except Exception:
        pass

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, interaction.user.id))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await interaction.response.send_message(f"🗑️ Deleted reminder **{reminder_id}**.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Reminder not found.", ephemeral=True)


# ---------- Lifecycle ----------
@bot.event
async def on_ready():
    init_db()
    await tree.sync()
    if not scheduler.running:
        scheduler.start()
        schedule_loaded_jobs()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info("Bot is ready.")


if __name__ == "__main__":
    bot.run(TOKEN)
