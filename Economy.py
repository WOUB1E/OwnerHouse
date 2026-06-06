import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


DB_PATH = Path(__file__).with_name("bot_data.sqlite3")
CURRENCY_ICON = "🍀"
CURRENCY_NAME = "клевер"


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db_sync() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economy (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )


def get_balance_sync(guild_id: int, user_id: int) -> int:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return int(row["balance"]) if row else 0


def set_balance_sync(guild_id: int, user_id: int, balance: int) -> int:
    current_time = now_ts()
    new_balance = max(0, balance)
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO economy (guild_id, user_id, balance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, new_balance, current_time, current_time),
        )
    return new_balance


def add_balance_sync(guild_id: int, user_id: int, amount: int) -> int:
    current_time = now_ts()
    with connect_db() as conn:
        row = conn.execute(
            "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        current_balance = int(row["balance"]) if row else 0
        new_balance = max(0, current_balance + amount)
        conn.execute(
            """
            INSERT INTO economy (guild_id, user_id, balance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, new_balance, current_time, current_time),
        )
    return new_balance


def transfer_sync(guild_id: int, sender_id: int, receiver_id: int, amount: int) -> tuple[bool, int, int]:
    if amount <= 0:
        return False, get_balance_sync(guild_id, sender_id), get_balance_sync(guild_id, receiver_id)

    current_time = now_ts()
    with connect_db() as conn:
        sender = conn.execute(
            "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
            (guild_id, sender_id),
        ).fetchone()
        sender_balance = int(sender["balance"]) if sender else 0
        if sender_balance < amount:
            receiver_balance = get_balance_sync(guild_id, receiver_id)
            return False, sender_balance, receiver_balance

        receiver = conn.execute(
            "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
            (guild_id, receiver_id),
        ).fetchone()
        receiver_balance = int(receiver["balance"]) if receiver else 0
        conn.execute(
            """
            INSERT INTO economy (guild_id, user_id, balance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
            """,
            (guild_id, sender_id, sender_balance - amount, current_time, current_time),
        )
        conn.execute(
            """
            INSERT INTO economy (guild_id, user_id, balance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
            """,
            (guild_id, receiver_id, receiver_balance + amount, current_time, current_time),
        )
        return True, sender_balance - amount, receiver_balance + amount


class EconomyAdminView(discord.ui.View):
    def __init__(self, cog: "Economy", target: discord.Member):
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
        embed = await self.cog.balance_embed(interaction.guild, self.target)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="+100 🍀", style=discord.ButtonStyle.success)
    async def add_small(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_balance_sync, interaction.guild_id, self.target.id, 100)
        await self.refresh(interaction)

    @discord.ui.button(label="+500 🍀", style=discord.ButtonStyle.success)
    async def add_big(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_balance_sync, interaction.guild_id, self.target.id, 500)
        await self.refresh(interaction)

    @discord.ui.button(label="-100 🍀", style=discord.ButtonStyle.secondary)
    async def remove_small(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(add_balance_sync, interaction.guild_id, self.target.id, -100)
        await self.refresh(interaction)

    @discord.ui.button(label="Сброс", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await asyncio.to_thread(set_balance_sync, interaction.guild_id, self.target.id, 0)
        await self.refresh(interaction)


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db_sync()

    async def balance_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        balance = await asyncio.to_thread(get_balance_sync, guild.id, member.id)
        embed = discord.Embed(
            description=f"Баланс: **{balance}** {CURRENCY_ICON} {CURRENCY_NAME}",
            color=discord.Color.from_rgb(35, 165, 90),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_footer(text=f"ID: {member.id}")
        return embed

    @app_commands.command(name="balance", description="Показать баланс")
    @app_commands.guild_only()
    @app_commands.describe(member="Участник")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Участник не найден.", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self.balance_embed(interaction.guild, target))

    @app_commands.command(name="pay", description="Передать клевер")
    @app_commands.guild_only()
    @app_commands.describe(member="Кому передать", amount="Сколько передать")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        if member.bot or member.id == interaction.user.id:
            await interaction.response.send_message("Нужно выбрать другого участника.", ephemeral=True)
            return

        ok, sender_balance, receiver_balance = await asyncio.to_thread(
            transfer_sync,
            interaction.guild.id,
            interaction.user.id,
            member.id,
            amount,
        )
        if not ok:
            await interaction.response.send_message(
                f"Не хватает клевера. Сейчас: **{sender_balance}** {CURRENCY_ICON}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"{interaction.user.mention} → {member.mention}: **{amount}** {CURRENCY_ICON}\n"
            f"Баланс: **{sender_balance}** / **{receiver_balance}** {CURRENCY_ICON}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="econ_admin", description="Админ-меню экономики")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Участник")
    async def economy_admin(self, interaction: discord.Interaction, member: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
            return
        embed = await self.balance_embed(interaction.guild, member)
        await interaction.response.send_message(embed=embed, view=EconomyAdminView(self, member), ephemeral=True)

    @economy_admin.error
    async def economy_admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("Нужно право `Управление сервером`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
