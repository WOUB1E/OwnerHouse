import asyncio
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


DB_PATH = Path(__file__).with_name("bot_data.sqlite3")

MESSAGE_COOLDOWN_SECONDS = 60
MESSAGE_XP_RANGE = (15, 25)
VOICE_XP_PER_MINUTE = 10
DAILY_WAIT_XP = 75
DAILY_VOICE_XP = 100
DAILY_VOICE_SECONDS = 10 * 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> float:
    return now_utc().timestamp()


def today_key() -> str:
    return now_utc().strftime("%Y-%m-%d")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db_sync() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                total_message_xp INTEGER NOT NULL DEFAULT 0,
                total_voice_xp INTEGER NOT NULL DEFAULT 0,
                last_message_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS level_daily (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                voice_active_seconds INTEGER NOT NULL DEFAULT 0,
                voice_10_done INTEGER NOT NULL DEFAULT 0,
                wait_guest_done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, day)
            )
            """
        )


def xp_for_level(level: int) -> int:
    return 100 * level * level


def level_from_xp(xp: int) -> int:
    if xp <= 0:
        return 0
    return int(math.sqrt(xp / 100))


def level_bounds(xp: int) -> tuple[int, int, int]:
    level = level_from_xp(xp)
    current_level_xp = xp_for_level(level)
    next_level_xp = xp_for_level(level + 1)
    return level, xp - current_level_xp, next_level_xp - current_level_xp


def progress_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * current / total)))
    return "█" * filled + "░" * (width - filled)


def add_xp_on_conn(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    amount: int,
    *,
    source: str = "admin",
) -> tuple[int, int, int]:
    current_time = now_ts()
    row = conn.execute(
        "SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    old_xp = int(row["xp"]) if row else 0
    new_xp = max(0, old_xp + amount)
    old_level = level_from_xp(old_xp)
    new_level = level_from_xp(new_xp)
    message_xp = amount if source == "message" and amount > 0 else 0
    voice_xp = amount if source == "voice" and amount > 0 else 0

    conn.execute(
        """
        INSERT INTO levels (
            guild_id, user_id, xp, total_message_xp, total_voice_xp,
            last_message_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            xp = excluded.xp,
            total_message_xp = levels.total_message_xp + ?,
            total_voice_xp = levels.total_voice_xp + ?,
            updated_at = excluded.updated_at
        """,
        (
            guild_id,
            user_id,
            new_xp,
            message_xp,
            voice_xp,
            current_time,
            current_time,
            message_xp,
            voice_xp,
        ),
    )
    return new_xp, old_level, new_level


def add_xp_sync(guild_id: int, user_id: int, amount: int, source: str = "admin") -> tuple[int, int, int]:
    with connect_db() as conn:
        return add_xp_on_conn(conn, guild_id, user_id, amount, source=source)


def set_xp_sync(guild_id: int, user_id: int, xp: int) -> tuple[int, int, int]:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        old_xp = int(row["xp"]) if row else 0
        new_xp = max(0, xp)
        current_time = now_ts()
        conn.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                xp = excluded.xp,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, new_xp, current_time, current_time),
        )
        return new_xp, level_from_xp(old_xp), level_from_xp(new_xp)


def get_profile_sync(guild_id: int, user_id: int) -> dict:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT xp, total_message_xp, total_voice_xp, last_message_at
            FROM levels
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        xp = int(row["xp"]) if row else 0
        higher = conn.execute(
            "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > ?",
            (guild_id, xp),
        ).fetchone()[0]

    level, current, needed = level_bounds(xp)
    return {
        "xp": xp,
        "level": level,
        "current": current,
        "needed": needed,
        "rank": int(higher) + 1,
        "message_xp": int(row["total_message_xp"]) if row else 0,
        "voice_xp": int(row["total_voice_xp"]) if row else 0,
    }


def try_award_message_xp_sync(guild_id: int, user_id: int) -> tuple[int, int, int] | None:
    current_time = now_ts()
    amount = random.randint(*MESSAGE_XP_RANGE)
    with connect_db() as conn:
        row = conn.execute(
            "SELECT xp, last_message_at FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        last_message_at = float(row["last_message_at"]) if row else 0
        if current_time - last_message_at < MESSAGE_COOLDOWN_SECONDS:
            return None

        new_xp, old_level, new_level = add_xp_on_conn(
            conn,
            guild_id,
            user_id,
            amount,
            source="message",
        )
        conn.execute(
            """
            UPDATE levels
            SET last_message_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (current_time, current_time, guild_id, user_id),
        )
        return amount, old_level, new_level


def ensure_daily_on_conn(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO level_daily (guild_id, user_id, day)
        VALUES (?, ?, ?)
        """,
        (guild_id, user_id, today_key()),
    )


def add_daily_voice_seconds_sync(guild_id: int, user_id: int, seconds: int) -> bool:
    with connect_db() as conn:
        ensure_daily_on_conn(conn, guild_id, user_id)
        row = conn.execute(
            """
            SELECT voice_active_seconds, voice_10_done
            FROM level_daily
            WHERE guild_id = ? AND user_id = ? AND day = ?
            """,
            (guild_id, user_id, today_key()),
        ).fetchone()
        current_seconds = int(row["voice_active_seconds"])
        if int(row["voice_10_done"]):
            conn.execute(
                """
                UPDATE level_daily
                SET voice_active_seconds = voice_active_seconds + ?
                WHERE guild_id = ? AND user_id = ? AND day = ?
                """,
                (seconds, guild_id, user_id, today_key()),
            )
            return False

        new_seconds = current_seconds + seconds
        done = new_seconds >= DAILY_VOICE_SECONDS
        conn.execute(
            """
            UPDATE level_daily
            SET voice_active_seconds = ?, voice_10_done = ?
            WHERE guild_id = ? AND user_id = ? AND day = ?
            """,
            (new_seconds, int(done), guild_id, user_id, today_key()),
        )
        if done:
            add_xp_on_conn(conn, guild_id, user_id, DAILY_VOICE_XP, source="voice")
        return done


def award_wait_daily_sync(guild_id: int, user_id: int) -> bool:
    with connect_db() as conn:
        ensure_daily_on_conn(conn, guild_id, user_id)
        row = conn.execute(
            """
            SELECT wait_guest_done
            FROM level_daily
            WHERE guild_id = ? AND user_id = ? AND day = ?
            """,
            (guild_id, user_id, today_key()),
        ).fetchone()
        if int(row["wait_guest_done"]):
            return False

        conn.execute(
            """
            UPDATE level_daily
            SET wait_guest_done = 1
            WHERE guild_id = ? AND user_id = ? AND day = ?
            """,
            (guild_id, user_id, today_key()),
        )
        add_xp_on_conn(conn, guild_id, user_id, DAILY_WAIT_XP, source="voice")
        return True


@dataclass
class VoiceSession:
    channel_id: int
    joined_at: datetime
    last_award_at: datetime


class LevelAdminView(discord.ui.View):
    def __init__(self, cog: "Levels", target: discord.Member):
        super().__init__(timeout=180)
        self.cog = cog
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        allowed = interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator
        if not allowed:
            await interaction.response.send_message("Нужно право `Управление сервером`.", ephemeral=True)
        return allowed

    async def refresh(self, interaction: discord.Interaction) -> None:
        embed = await self.cog.level_embed(interaction.guild, self.target)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="+100 XP", style=discord.ButtonStyle.success)
    async def add_small(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_xp_sync, interaction.guild_id, self.target.id, 100, "admin")
        await self.refresh(interaction)

    @discord.ui.button(label="+500 XP", style=discord.ButtonStyle.success)
    async def add_big(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_xp_sync, interaction.guild_id, self.target.id, 500, "admin")
        await self.refresh(interaction)

    @discord.ui.button(label="-100 XP", style=discord.ButtonStyle.secondary)
    async def remove_small(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_xp_sync, interaction.guild_id, self.target.id, -100, "admin")
        await self.refresh(interaction)

    @discord.ui.button(label="Сброс", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(set_xp_sync, interaction.guild_id, self.target.id, 0)
        await self.refresh(interaction)


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_sessions: dict[tuple[int, int], VoiceSession] = {}
        self.waiting_by_channel: dict[tuple[int, int], set[int]] = {}
        init_db_sync()
        self.voice_xp_loop.start()

    def cog_unload(self) -> None:
        self.voice_xp_loop.cancel()

    def is_active_voice(self, member: discord.Member, state: discord.VoiceState | None = None) -> bool:
        state = state or member.voice
        if state is None or state.channel is None:
            return False
        if member.guild.afk_channel and state.channel.id == member.guild.afk_channel.id:
            return False
        return not state.self_mute and not state.mute

    def ensure_voice_session(self, member: discord.Member) -> VoiceSession | None:
        if member.voice is None or member.voice.channel is None:
            return None
        key = (member.guild.id, member.id)
        session = self.voice_sessions.get(key)
        if session is None or session.channel_id != member.voice.channel.id:
            session = VoiceSession(member.voice.channel.id, now_utc(), now_utc())
            self.voice_sessions[key] = session
        return session

    def remove_waiter(self, member: discord.Member, channel: discord.abc.GuildChannel | None) -> None:
        if channel is None:
            return
        waiters = self.waiting_by_channel.get((member.guild.id, channel.id))
        if not waiters:
            return
        waiters.discard(member.id)
        if not waiters:
            self.waiting_by_channel.pop((member.guild.id, channel.id), None)

    async def handle_voice_join_wait(self, member: discord.Member, channel: discord.VoiceChannel | discord.StageChannel | None) -> None:
        if channel is None:
            return

        key = (member.guild.id, channel.id)
        waiters = self.waiting_by_channel.get(key, set())
        for user_id in list(waiters):
            if user_id != member.id:
                await asyncio.to_thread(award_wait_daily_sync, member.guild.id, user_id)
                waiters.discard(user_id)

        if waiters:
            self.waiting_by_channel[key] = waiters
        else:
            self.waiting_by_channel.pop(key, None)

        non_bot_members = [voice_member for voice_member in channel.members if not voice_member.bot]
        if len(non_bot_members) == 1 and non_bot_members[0].id == member.id:
            self.waiting_by_channel.setdefault(key, set()).add(member.id)

    async def level_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        profile = await asyncio.to_thread(get_profile_sync, guild.id, member.id)
        bar = progress_bar(profile["current"], profile["needed"])
        embed = discord.Embed(
            description=(
                f"**Уровень {profile['level']}** · `{profile['current']}/{profile['needed']}` XP\n"
                f"`{bar}`\n"
                f"Всего: `{profile['xp']}` XP · место `#{profile['rank']}`"
            ),
            color=discord.Color.from_rgb(88, 101, 242),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_footer(text=f"Текст: {profile['message_xp']} XP · Войс: {profile['voice_xp']} XP")
        return embed

    @commands.Cog.listener()
    async def on_ready(self):
        self.voice_sessions.clear()
        self.waiting_by_channel.clear()
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                non_bot_members = [member for member in channel.members if not member.bot]
                if len(non_bot_members) == 1:
                    self.waiting_by_channel.setdefault((guild.id, channel.id), set()).add(non_bot_members[0].id)
                for member in non_bot_members:
                    self.ensure_voice_session(member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.author, discord.Member):
            return
        await asyncio.to_thread(try_award_message_xp_sync, message.guild.id, message.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        if before.channel != after.channel:
            self.remove_waiter(member, before.channel)
            await self.handle_voice_join_wait(member, after.channel)

        key = (member.guild.id, member.id)
        if after.channel is None:
            self.voice_sessions.pop(key, None)
            return

        session = self.ensure_voice_session(member)
        if session is None:
            return

        if before.channel != after.channel:
            session.joined_at = now_utc()
            session.last_award_at = now_utc()
        elif before.self_mute != after.self_mute or before.mute != after.mute:
            session.last_award_at = now_utc()

    @tasks.loop(seconds=60)
    async def voice_xp_loop(self):
        current_time = now_utc()
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    session = self.ensure_voice_session(member)
                    if session is None:
                        continue
                    if not self.is_active_voice(member):
                        session.last_award_at = current_time
                        continue

                    elapsed = int((current_time - session.last_award_at).total_seconds())
                    if elapsed < 60:
                        continue

                    minutes = elapsed // 60
                    awarded_seconds = minutes * 60
                    session.last_award_at += timedelta(seconds=awarded_seconds)
                    await asyncio.to_thread(
                        add_xp_sync,
                        guild.id,
                        member.id,
                        minutes * VOICE_XP_PER_MINUTE,
                        "voice",
                    )
                    await asyncio.to_thread(add_daily_voice_seconds_sync, guild.id, member.id, awarded_seconds)

    @voice_xp_loop.before_loop
    async def before_voice_xp_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="level", description="Показать свой уровень")
    @app_commands.guild_only()
    @app_commands.describe(member="Участник")
    async def level(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Участник не найден.", ephemeral=True)
            return
        embed = await self.level_embed(interaction.guild, target)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lvl_admin", description="Админ-меню уровней")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Участник")
    async def level_admin(self, interaction: discord.Interaction, member: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        embed = await self.level_embed(interaction.guild, member)
        await interaction.response.send_message(embed=embed, view=LevelAdminView(self, member), ephemeral=True)

    @level_admin.error
    async def level_admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("Нужно право `Управление сервером`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Levels(bot))
