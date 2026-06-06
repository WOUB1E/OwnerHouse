import asyncio
from collections.abc import Iterable

import discord
from discord.ext import commands


ACTIVITY_LOG_CHANNEL_NAME = "🗈》активности"
USER_LOG_CHANNEL_NAME = "🗈》пользовательские"
VOICE_LOG_CHANNEL_NAME = "🗈》голосовые"
TEXT_LOG_CHANNEL_NAME = "🗈》текстовые"

AUDIT_LOOKBACK_SECONDS = 12


def add_user_author(embed: discord.Embed, user: discord.Member | discord.User) -> discord.Embed:
    name = getattr(user, "display_name", None) or user.name
    embed.set_author(name=name, icon_url=user.display_avatar.url)
    return embed


def compact_user(user: discord.Member | discord.User) -> str:
    return f"<@{user.id}>"


def channel_name(channel: discord.abc.GuildChannel | None) -> str:
    if channel is None:
        return "`нет канала`"
    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
        return channel.mention
    return f"`{channel.name}`"


def truncate(value: str, limit: int = 900) -> str:
    value = discord.utils.escape_mentions((value or "").strip())
    if not value:
        return "`пусто`"
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def join_values(values: Iterable[str], limit: int = 700) -> str:
    text = ", ".join(values)
    return truncate(text, limit)


def audit_entry_age(entry: discord.AuditLogEntry) -> float:
    return abs((discord.utils.utcnow() - entry.created_at).total_seconds())


def audit_target_id(entry: discord.AuditLogEntry) -> int | None:
    target = getattr(entry, "target", None)
    return getattr(target, "id", None)


def audit_extra_channel_id(entry: discord.AuditLogEntry) -> int | None:
    extra = getattr(entry, "extra", None)
    channel = getattr(extra, "channel", None)
    return getattr(channel, "id", None)


class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def log_channel(self, guild: discord.Guild, name: str) -> discord.TextChannel | None:
        return discord.utils.get(guild.text_channels, name=name)

    async def target_log_channel(
        self,
        guild: discord.Guild,
        _source_channel: discord.abc.GuildChannel | None,
        default_channel_name: str,
    ) -> discord.TextChannel | None:
        return self.log_channel(guild, default_channel_name)

    async def send_embed(
        self,
        channel: discord.TextChannel | None,
        embed: discord.Embed,
        user: discord.Member | discord.User,
    ) -> None:
        if channel is None:
            return
        add_user_author(embed, user)
        embed.timestamp = embed.timestamp or discord.utils.utcnow()
        embed.set_footer(text=f"ID: {user.id}")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            return

    async def recent_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        member: discord.Member | discord.User,
        *,
        channel: discord.abc.GuildChannel | None = None,
        strict_target: bool = False,
        limit: int = 5,
    ) -> discord.AuditLogEntry | None:
        try:
            async for entry in guild.audit_logs(action=action, limit=limit):
                if audit_entry_age(entry) > AUDIT_LOOKBACK_SECONDS:
                    continue
                if entry.user and entry.user.id == member.id:
                    continue

                target_id = audit_target_id(entry)
                if target_id == member.id:
                    return entry
                if strict_target:
                    continue

                extra_channel_id = audit_extra_channel_id(entry)
                if channel and extra_channel_id and extra_channel_id == channel.id:
                    return entry
                if target_id is None:
                    return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = discord.Embed(
            description=f"📥 Зашел на сервер · {compact_user(member)}",
            color=discord.Color.green(),
        )
        await self.send_embed(self.log_channel(member.guild, USER_LOG_CHANNEL_NAME), embed, member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel = self.log_channel(member.guild, USER_LOG_CHANNEL_NAME)
        if channel is None:
            return

        await asyncio.sleep(0.6)
        entry = await self.recent_audit_entry(
            member.guild,
            discord.AuditLogAction.kick,
            member,
            strict_target=True,
        )
        if entry:
            reason = truncate(entry.reason or "не указана", 400)
            executor = compact_user(entry.user) if entry.user else "`неизвестно`"
            embed = discord.Embed(
                description=f"👢 Кик · модератор {executor}\nПричина: {reason}",
                color=discord.Color.orange(),
            )
            await self.send_embed(channel, embed, member)
            return

        embed = discord.Embed(
            description=f"📤 Покинул сервер · {compact_user(member)}",
            color=discord.Color.red(),
        )
        await self.send_embed(channel, embed, member)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        channel = self.log_channel(guild, USER_LOG_CHANNEL_NAME)
        if channel is None:
            return

        await asyncio.sleep(0.6)
        entry = await self.recent_audit_entry(
            guild,
            discord.AuditLogAction.ban,
            user,
            strict_target=True,
        )
        executor = compact_user(entry.user) if entry and entry.user else "`неизвестно`"
        reason = truncate(entry.reason if entry else "не указана", 400)
        embed = discord.Embed(
            description=f"🔨 Бан · модератор {executor}\nПричина: {reason}",
            color=discord.Color.dark_red(),
        )
        await self.send_embed(channel, embed, user)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = discord.Embed(
            description=f"🕊️ Разбан · {compact_user(user)}",
            color=discord.Color.green(),
        )
        await self.send_embed(self.log_channel(guild, USER_LOG_CHANNEL_NAME), embed, user)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        channel = self.log_channel(after.guild, USER_LOG_CHANNEL_NAME)
        if channel is None:
            return

        if before.nick != after.nick:
            old = truncate(before.nick or before.name, 120)
            new = truncate(after.nick or after.name, 120)
            embed = discord.Embed(
                description=f"✏️ Ник · `{old}` → `{new}`",
                color=discord.Color.blue(),
            )
            await self.send_embed(channel, embed, after)

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                until = f"<t:{int(after.timed_out_until.timestamp())}:R>"
                embed = discord.Embed(
                    description=f"⏳ Таймаут · до {until}",
                    color=discord.Color.orange(),
                )
            else:
                embed = discord.Embed(
                    description="🔄 Таймаут снят",
                    color=discord.Color.green(),
                )
            await self.send_embed(channel, embed, after)

        before_roles = set(before.roles)
        after_roles = set(after.roles)
        added = [role.mention for role in after_roles - before_roles if not role.is_default()]
        removed = [role.mention for role in before_roles - after_roles if not role.is_default()]
        if added or removed:
            lines = []
            if added:
                lines.append(f"+ {join_values(added[:8])}")
            if removed:
                lines.append(f"- {join_values(removed[:8])}")
            embed = discord.Embed(
                description="🎭 Роли · " + "\n".join(lines),
                color=discord.Color.blurple(),
            )
            await self.send_embed(channel, embed, after)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        channel = await self.target_log_channel(message.guild, message.channel, TEXT_LOG_CHANNEL_NAME)
        if channel is None:
            return

        parts = []
        if message.content:
            parts.append(message.content)
        if message.attachments:
            filenames = ", ".join(attachment.filename for attachment in message.attachments[:5])
            parts.append(f"Вложения: {filenames}")

        embed = discord.Embed( 
            description=f"🗑️ Удалено · {channel_name(message.channel)}",
            color=discord.Color.red(),
        )
        embed.add_field(name="Текст", value=truncate("\n".join(parts), 1000), inline=False)
        await self.send_embed(channel, embed, message.author)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild:
            return
        if before.content == after.content:
            return

        channel = await self.target_log_channel(before.guild, before.channel, TEXT_LOG_CHANNEL_NAME)
        if channel is None:
            return

        embed = discord.Embed(
            description=f"📝 Изменено · {channel_name(before.channel)} · [перейти]({after.jump_url})",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Было", value=truncate(before.content, 850), inline=False)
        embed.add_field(name="Стало", value=truncate(after.content, 850), inline=False)
        await self.send_embed(channel, embed, before.author)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        source_channel = after.channel or before.channel
        channel = await self.target_log_channel(member.guild, source_channel, VOICE_LOG_CHANNEL_NAME)
        if channel is None:
            return

        if before.channel is None and after.channel is not None:
            embed = discord.Embed(
                description=f"📞 Зашел · {channel_name(after.channel)}",
                color=discord.Color.green(),
            )
            await self.send_embed(channel, embed, member)
            return

        if before.channel is not None and after.channel is None:
            await asyncio.sleep(0.8)
            entry = await self.recent_audit_entry(
                member.guild,
                discord.AuditLogAction.member_disconnect,
                member,
                channel=before.channel,
            )
            if entry:
                executor = compact_user(entry.user) if entry.user else "`неизвестно`"
                embed = discord.Embed(
                    description=f"🚷 Отключен · {channel_name(before.channel)} · модератор {executor}",
                    color=discord.Color.orange(),
                )
            else:
                embed = discord.Embed(
                    description=f"☎️ Вышел · {channel_name(before.channel)}",
                    color=discord.Color.red(),
                )
            await self.send_embed(channel, embed, member)
            return

        if before.channel != after.channel and after.channel is not None:
            await asyncio.sleep(0.8)
            entry = await self.recent_audit_entry(
                member.guild,
                discord.AuditLogAction.member_move,
                member,
                channel=after.channel,
            )
            route = f"{channel_name(before.channel)} → {channel_name(after.channel)}"
            if entry:
                executor = compact_user(entry.user) if entry.user else "`неизвестно`"
                embed = discord.Embed(
                    description=f"🔀 Перемещен · {route} · модератор {executor}",
                    color=discord.Color.blue(),
                )
            else:
                embed = discord.Embed(
                    description=f"🚶 Перешел · {route}",
                    color=discord.Color.teal(),
                )
            await self.send_embed(channel, embed, member)
            return

        if before.self_deaf != after.self_deaf:
            state = "выключил звук" if after.self_deaf else "включил звук"
            embed = discord.Embed(
                description=f"🎧 Сам · {state}",
                color=discord.Color.dark_grey(),
            )
            await self.send_embed(channel, embed, member)
            return

        if before.deaf != after.deaf:
            state = "выключили звук" if after.deaf else "включили звук"
            embed = discord.Embed(
                description=f"⛔ Админ · {state}",
                color=discord.Color.orange(),
            )
            await self.send_embed(channel, embed, member)
            return

        if before.self_mute != after.self_mute:
            state = "замутился" if after.self_mute else "размутился"
            embed = discord.Embed(
                description=f"🎙️ Сам · {state}",
                color=discord.Color.dark_grey(),
            )
            await self.send_embed(channel, embed, member)
            return

        if before.mute != after.mute:
            state = "замучен" if after.mute else "размучен"
            embed = discord.Embed(
                description=f"🔇 Админ · {state}",
                color=discord.Color.orange(),
            )
            await self.send_embed(channel, embed, member)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return

        channel = self.log_channel(after.guild, ACTIVITY_LOG_CHANNEL_NAME)
        if channel is None:
            return

        items: list[str] = []
        if before.status != after.status:
            status_names = {
                discord.Status.online: "🟢",
                discord.Status.idle: "🌙",
                discord.Status.dnd: "⛔",
                discord.Status.offline: "⚫",
            }
            old_status = status_names.get(before.status, str(before.status))
            new_status = status_names.get(after.status, str(after.status))
            device = "👤"
            if before.mobile_status != after.mobile_status:
                device = "📱"
            elif before.desktop_status != after.desktop_status:
                device = "💻"
            elif before.web_status != after.web_status:
                device = "🌐"
            items.append(f"{device} `{old_status}`→`{new_status}`")

        before_acts = [
            activity.name
            for activity in before.activities
            if activity.name and not isinstance(activity, discord.CustomActivity)
        ]
        after_acts = [
            activity.name
            for activity in after.activities
            if activity.name and not isinstance(activity, discord.CustomActivity)
        ]
        started = [activity for activity in after_acts if activity not in before_acts]
        finished = [activity for activity in before_acts if activity not in after_acts]
        items.extend(f"🎮 `{truncate(activity, 80)}`" for activity in started[:3])
        items.extend(f"🛑 `{truncate(activity, 80)}`" for activity in finished[:3])

        if not items:
            return

        text = f"-# {compact_user(after)} · " + " · ".join(items)
        await channel.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(bot))
