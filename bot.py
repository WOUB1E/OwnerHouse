import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


CONFIG_PATH = Path(__file__).with_name("rooms_config.json")
ENV_PATH = Path(__file__).with_name(".env")
DASHBOARD_CHANNEL_NAME = "room-dashboard"
LOGS_CHANNEL_NAME = "🗈》комнаты"
ROOM_TEXT_CHANNEL_NAME = "chat"
ROOM_VOICE_CHANNEL_NAME = "voice"
ROOM_VOICE_USER_LIMIT = 99
ARCHIVED_ROOM_PREFIX = "archived-"
AUTO_ARCHIVE_AFTER = timedelta(days=7)
AUTO_DELETE_ARCHIVED_AFTER = timedelta(days=21)
ROOM_MAINTENANCE_INTERVAL_MINUTES = 30

COLOR_DASHBOARD = discord.Color.from_rgb(88, 101, 242)
COLOR_SETTINGS_OPEN = discord.Color.from_rgb(35, 165, 90)
COLOR_SETTINGS_PRIVATE = discord.Color.from_rgb(32, 102, 148)
COLOR_ARCHIVED = discord.Color.from_rgb(116, 127, 141)
COLOR_REQUEST_PENDING = discord.Color.from_rgb(250, 166, 26)
COLOR_DANGER = discord.Color.from_rgb(237, 66, 69)


# Настройка интентов (разрешений)
intents = discord.Intents.default()
intents.members = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Persistent view: кнопки дэшборда переживают перезапуск бота.
        self.add_view(RoomDashboardView())
        self.add_view(RoomPanelView())
        for request_id in iter_pending_request_ids():
            self.add_view(RoomRequestDecisionView(request_id))
        if not room_maintenance.is_running():
            room_maintenance.start()
        await self.tree.sync()
        print(f"Синхронизированы команды для {self.user}")


bot = MyBot()


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"guilds": {}}

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        backup_path = CONFIG_PATH.with_suffix(".broken.json")
        CONFIG_PATH.replace(backup_path)
        print(f"Конфиг поврежден и сохранен как {backup_path.name}")
        return {"guilds": {}}

    data.setdefault("guilds", {})
    return data


CONFIG = load_config()


def save_config() -> None:
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(CONFIG, file, ensure_ascii=False, indent=2)
    temp_path.replace(CONFIG_PATH)


def guild_config(guild: discord.Guild) -> dict:
    guilds = CONFIG.setdefault("guilds", {})
    config = guilds.setdefault(
        str(guild.id),
        {
            "dashboard_channel_id": None,
            "dashboard_message_id": None,
            "logs_channel_id": None,
            "rooms": {},
            "requests": {},
        },
    )
    config.setdefault("rooms", {})
    config.setdefault("requests", {})
    return config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def discord_time(value: str | None, style: str = "R") -> str:
    parsed = parse_utc(value)
    if not parsed:
        return "`нет данных`"
    return f"<t:{int(parsed.timestamp())}:{style}>"


def guild_icon_url(guild: discord.Guild) -> str | None:
    if guild.icon:
        return guild.icon.with_size(256).url
    return None


def room_counts(guild: discord.Guild) -> dict[str, int]:
    rooms = get_all_rooms(guild)
    active_rooms = [room for room in rooms if not room.get("archived")]
    return {
        "total": len(rooms),
        "active": len(active_rooms),
        "open": sum(1 for room in active_rooms if room.get("status") == "open"),
        "private": sum(1 for room in active_rooms if room.get("status") != "open"),
        "archived": len(rooms) - len(active_rooms),
    }


def pending_requests_count(guild: discord.Guild) -> int:
    return sum(
        1
        for request in guild_config(guild).get("requests", {}).values()
        if request.get("status") == "pending"
    )


def room_status_title(room: dict) -> str:
    if room.get("archived"):
        return "Архив"
    if room.get("status") == "open":
        return "Открытая"
    return "Приватная"


def room_color(room: dict) -> discord.Color:
    if room.get("archived"):
        return COLOR_ARCHIVED
    if room.get("status") == "open":
        return COLOR_SETTINGS_OPEN
    return COLOR_SETTINGS_PRIVATE


def user_mention(user_id: int | str) -> str:
    return f"<@{int(user_id)}>"


def channel_label(guild: discord.Guild, channel_id: int | None) -> str:
    if not channel_id:
        return "не найден"

    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        return channel.mention
    if isinstance(channel, discord.VoiceChannel):
        return f"`{channel.name}`"
    if isinstance(channel, discord.CategoryChannel):
        return f"`{channel.name}`"
    return f"`{channel_id}`"


def safe_room_name(member: discord.Member | discord.User) -> str:
    base_name = member.display_name if isinstance(member, discord.Member) else member.name
    safe_name = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ_-]+", "-", base_name).strip("-").lower()
    return safe_name[:50] or str(member.id)


def sanitize_channel_name(name: str) -> str:
    safe_name = re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ_-]+", "-", name.strip()).strip("-").lower()
    return safe_name[:90]


def normalize_guest_ids(room: dict) -> list[int]:
    guest_ids = []
    for guest_id in room.get("guest_ids", []):
        try:
            normalized_id = int(guest_id)
        except (TypeError, ValueError):
            continue
        if normalized_id not in guest_ids and normalized_id != int(room["owner_id"]):
            guest_ids.append(normalized_id)
    room["guest_ids"] = guest_ids
    return guest_ids


def normalize_extra_channel_ids(room: dict) -> list[int]:
    channel_ids = []
    for channel_id in room.get("extra_channel_ids", []):
        try:
            normalized_id = int(channel_id)
        except (TypeError, ValueError):
            continue
        if normalized_id not in channel_ids:
            channel_ids.append(normalized_id)
    room["extra_channel_ids"] = channel_ids
    return channel_ids


def ensure_room_defaults(room: dict) -> None:
    normalize_guest_ids(room)
    normalize_extra_channel_ids(room)
    room.setdefault("status", "private")
    room.setdefault("archived", False)
    room.setdefault("last_active_at", room.get("updated_at") or room.get("created_at") or utc_now())


def archived_room_key(room: dict) -> str:
    return f"archived:{int(room['category_id'])}"


def find_room_key(guild: discord.Guild, room: dict) -> str | None:
    category_id = int(room.get("category_id", 0))
    for key, existing_room in guild_config(guild)["rooms"].items():
        if int(existing_room.get("category_id", 0)) == category_id:
            return key
    return None


def get_room_by_key(guild: discord.Guild, room_key: str) -> dict | None:
    room = guild_config(guild)["rooms"].get(str(room_key))
    if not room:
        return None

    category = guild.get_channel(int(room.get("category_id", 0)))
    if category is None:
        guild_config(guild)["rooms"].pop(str(room_key), None)
        save_config()
        return None

    ensure_room_defaults(room)
    return room


def get_room_by_category(guild: discord.Guild, category_id: int) -> dict | None:
    for room in guild_config(guild)["rooms"].values():
        if int(room.get("category_id", 0)) == int(category_id):
            ensure_room_defaults(room)
            return room
    return None


def get_room_by_channel(guild: discord.Guild, channel: discord.abc.GuildChannel) -> tuple[str, dict] | tuple[None, None]:
    channel_id = int(channel.id)
    category_id = int(channel.category_id or 0) if hasattr(channel, "category_id") else 0

    for key, room in guild_config(guild)["rooms"].items():
        ensure_room_defaults(room)
        room_channel_ids = {
            int(room.get("category_id", 0)),
            int(room.get("text_channel_id", 0)),
            int(room.get("voice_channel_id", 0)),
            *normalize_extra_channel_ids(room),
        }
        if channel_id in room_channel_ids or category_id == int(room.get("category_id", 0)):
            return key, room

    return None, None


def move_room_to_key(guild: discord.Guild, room: dict, new_key: str) -> None:
    rooms = guild_config(guild)["rooms"]
    old_key = find_room_key(guild, room)
    if old_key and old_key != new_key:
        rooms.pop(old_key, None)
    rooms[str(new_key)] = room


def iter_pending_request_ids() -> list[str]:
    request_ids = []
    for guild in CONFIG.get("guilds", {}).values():
        for request_id, request in guild.get("requests", {}).items():
            if request.get("status") == "pending":
                request_ids.append(str(request_id))
    return request_ids


def get_room_for_owner(guild: discord.Guild, owner_id: int) -> dict | None:
    config = guild_config(guild)
    room = config["rooms"].get(str(owner_id))
    if not room:
        return None

    if room.get("archived"):
        move_room_to_key(guild, room, archived_room_key(room))
        save_config()
        return None

    category = guild.get_channel(int(room.get("category_id", 0)))
    if category is None:
        config["rooms"].pop(str(owner_id), None)
        save_config()
        return None

    ensure_room_defaults(room)
    return room


def get_all_rooms(guild: discord.Guild) -> list[dict]:
    config = guild_config(guild)
    rooms = []

    for owner_id, room in list(config["rooms"].items()):
        category = guild.get_channel(int(room.get("category_id", 0)))
        if category is None:
            config["rooms"].pop(owner_id, None)
            continue

        ensure_room_defaults(room)
        if room.get("archived") and owner_id == str(room.get("owner_id")):
            move_room_to_key(guild, room, archived_room_key(room))
        rooms.append(room)

    save_config()
    return rooms


def is_room_admin(user: object) -> bool:
    if not isinstance(user, discord.Member):
        return False

    permissions = user.guild_permissions
    return permissions.administrator or permissions.manage_channels


async def fetch_member(guild: discord.Guild, member_id: int) -> discord.Member | None:
    member = guild.get_member(int(member_id))
    if member:
        return member

    try:
        return await guild.fetch_member(int(member_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def everyone_overwrite(status: str) -> discord.PermissionOverwrite:
    is_open = status == "open"
    return discord.PermissionOverwrite(
        view_channel=is_open,
        read_message_history=False,
        send_messages=is_open,
        connect=is_open,
        speak=is_open,
    )


def guest_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        attach_files=True,
        embed_links=True,
        connect=True,
        speak=True,
        stream=True,
        use_voice_activation=True,
    )


def owner_overwrite() -> discord.PermissionOverwrite:
    overwrite = guest_overwrite()
    overwrite.manage_webhooks = True
    overwrite.manage_messages = True
    return overwrite


def bot_overwrite() -> discord.PermissionOverwrite:
    overwrite = owner_overwrite()
    overwrite.manage_channels = True
    overwrite.manage_permissions = True
    return overwrite


def archived_overwrites(guild: discord.Guild) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    if guild.me:
        overwrites[guild.me] = bot_overwrite()

    return overwrites


async def build_room_overwrites(
    guild: discord.Guild,
    owner_id: int,
    guest_ids: list[int],
    status: str,
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: everyone_overwrite(status),
    }

    if guild.me:
        overwrites[guild.me] = bot_overwrite()

    owner = await fetch_member(guild, owner_id)
    if owner:
        overwrites[owner] = owner_overwrite()

    for guest_id in guest_ids:
        guest = await fetch_member(guild, guest_id)
        if guest and guest.id != owner_id:
            overwrites[guest] = guest_overwrite()

    return overwrites


async def apply_room_permissions(
    guild: discord.Guild,
    room: dict,
    reason: str,
) -> None:
    category = guild.get_channel(int(room["category_id"]))
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Категория комнаты не найдена")

    ensure_room_defaults(room)
    if room.get("archived"):
        overwrites = archived_overwrites(guild)
    else:
        overwrites = await build_room_overwrites(
            guild=guild,
            owner_id=int(room["owner_id"]),
            guest_ids=room["guest_ids"],
            status=room.get("status", "private"),
        )

    await category.edit(overwrites=overwrites, reason=reason)

    channel_ids = [
        room.get("text_channel_id"),
        room.get("voice_channel_id"),
        *normalize_extra_channel_ids(room),
    ]
    for channel_id in channel_ids:
        if not channel_id:
            continue

        channel = guild.get_channel(int(channel_id))
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            await channel.edit(sync_permissions=True, reason=reason)


def dashboard_embed(guild: discord.Guild) -> discord.Embed:
    config = guild_config(guild)
    counts = room_counts(guild)
    pending_count = pending_requests_count(guild)

    embed = discord.Embed(
        title="🏡 ┃ Уютный Домик — Личные Комнаты",
        description=(
            "> *Создай свой приватный уголок для общения, позови друзей "
            "и настрой доступ без возни с ролями.*"
        ),
        color=COLOR_DASHBOARD,
    )
    icon_url = guild_icon_url(guild)
    if icon_url:
        embed.set_thumbnail(url=icon_url)

    embed.add_field(
        name="📊 Статистика системы",
        value=(
            f"🟢 **Активные комнаты:** `{counts['active']}`\n"
            f"🔓 Открытые: `{counts['open']}`  |  🔒 Приватные: `{counts['private']}`\n"
            f"📦 **В архиве:** `{counts['archived']}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚀 Как это работает?",
        value=(
            "1️⃣ Нажми **Создать комнату**, чтобы открыть личную категорию.\n"
            "2️⃣ Используй **Настройки**, чтобы позвать друзей или изменить режим.\n"
            "3️⃣ Заявки на новые каналы попадут администрации."
        ),
        inline=False,
    )
    embed.add_field(
        name="🧾 Список заявок",
        value=(
            f"Ожидают решения: `{pending_count}`\n"
        ),
        inline=False,
    )
    embed.set_footer(text="Уютный Домик")
    return embed


def admin_panel_embed(guild: discord.Guild) -> discord.Embed:
    counts = room_counts(guild)
    pending_count = pending_requests_count(guild)

    embed = discord.Embed(
        title="🗝️ ┃ Админ-панель комнат",
        description="> *Выбери комнату из списка ниже, чтобы открыть управление*",
        color=COLOR_REQUEST_PENDING,
    )
    icon_url = guild_icon_url(guild)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    embed.add_field(name="🏠 Комнаты", value=f"Всего: `{counts['total']}`\nАктивные: `{counts['active']}`\nАрхив: `{counts['archived']}`", inline=True)
    embed.add_field(name="🔐 Режимы", value=f"Открытые: `{counts['open']}`\nПриватные: `{counts['private']}`", inline=True)
    embed.add_field(name="🧾 Заявки", value=f"Ожидают решения: `{pending_count}`", inline=True)
    embed.set_footer(text="Все решения администрации сохраняются в room-logs.")
    return embed


def settings_embed(guild: discord.Guild, room: dict) -> discord.Embed:
    ensure_room_defaults(room)
    owner_id = int(room["owner_id"])
    owner = guild.get_member(owner_id)
    guest_ids = room["guest_ids"]
    status = room.get("status", "private")
    is_open = status == "open"

    if room.get("archived"):
        status_line = "📦 **Статус:** `В архиве`"
    else:
        status_line = (
            "🔓 **Статус:** `Открытая`"
            if is_open
            else "🔒 **Статус:** `Приватная`"
        )
    guests_text = ", ".join(user_mention(guest_id) for guest_id in guest_ids) if guest_ids else "`гостей пока нет`"
    extra_channels = [
        channel_label(guild, channel_id)
        for channel_id in normalize_extra_channel_ids(room)
        if guild.get_channel(channel_id)
    ]

    embed = discord.Embed(
        title="🛠️ Управление Личной Комнатой",
        description=f"{status_line}\n⏱️ Активность: {discord_time(room.get('last_active_at'), 'R')}",
        color=room_color(room),
        timestamp=datetime.now(timezone.utc),
    )
    if owner:
        embed.set_author(name=f"Владелец: {owner.display_name}", icon_url=owner.display_avatar.url)
    else:
        embed.set_author(name=f"Владелец: {owner_id}")

    embed.add_field(
        name="📍 Расположение",
        value=(
            f"Категория: {channel_label(guild, room.get('category_id'))}\n"
            f"Создана: {discord_time(room.get('created_at'), 'd')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="📂 Твои каналы",
        value=(
            f"💬 Текстовый чат: {channel_label(guild, room.get('text_channel_id'))}\n"
            f"🔊 Голосовой канал: {channel_label(guild, room.get('voice_channel_id'))}\n"
            f"➕ Дополнительно: {', '.join(extra_channels)[:700] if extra_channels else '*нет дополнительных каналов*'}"
        ),
        inline=False,
    )
    if guest_ids:
        guests_text = "\n".join(f"• {user_mention(guest_id)}" for guest_id in guest_ids[:10])
        if len(guest_ids) > 10:
            guests_text += f"\n*...и еще {len(guest_ids) - 10} пользователей*"
    else:
        guests_text = "*В этой комнате еще нет гостей.*"
    embed.add_field(name=f"👥 Приглашенные гости ({len(guest_ids)})", value=guests_text[:1024], inline=False)
    embed.add_field(
        name="🌙 Информация",
        value=(
            f"Неактивная комната уходит в архив через `{AUTO_ARCHIVE_AFTER.days} дн.`\n"
            f"Архив хранится `{AUTO_DELETE_ARCHIVED_AFTER.days} дн.`"
        ),
        inline=False,
    )
    embed.set_footer(text="Используй кнопки ниже для настройки")
    return embed


def room_panel_embed(guild: discord.Guild, room: dict) -> discord.Embed:
    ensure_room_defaults(room)
    owner_id = int(room["owner_id"])
    status = room_status_title(room)
    guest_count = len(room["guest_ids"])
    extra_count = len(normalize_extra_channel_ids(room))

    if room.get("archived"):
        status_line = "📦 `архив`"
    elif room.get("status") == "open":
        status_line = "🔓 `открыта`"
    else:
        status_line = "🔒 `приватная`"

    embed = discord.Embed(
        title="🏠 Панель комнаты",
        description=(
            f"{status_line} · хозяин {user_mention(owner_id)}\n"
            "Все основные действия по комнате собраны здесь."
        ),
        color=room_color(room),
    )
    embed.add_field(
        name="Быстрый доступ",
        value=(
            f"💬 {channel_label(guild, room.get('text_channel_id'))}\n"
            f"🔊 {channel_label(guild, room.get('voice_channel_id'))}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Состояние",
        value=f"Гостей: `{guest_count}`\nДоп. каналов: `{extra_count}`\nРежим: {status_line}",
        inline=True,
    )
    return embed


async def ensure_logs_channel(guild: discord.Guild) -> discord.TextChannel | None:
    config = guild_config(guild)
    channel_id = config.get("logs_channel_id")
    channel = guild.get_channel(int(channel_id or 0))

    if isinstance(channel, discord.TextChannel):
        return channel

    channel = discord.utils.get(guild.text_channels, name=LOGS_CHANNEL_NAME)
    if channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

        channel = await guild.create_text_channel(
            LOGS_CHANNEL_NAME,
            overwrites=overwrites,
            reason="Создание канала логов личных комнат",
        )

    config["logs_channel_id"] = channel.id
    save_config()
    return channel


async def send_room_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: discord.Color,
    fields: list[tuple[str, str]] | None = None,
) -> None:
    try:
        channel = await ensure_logs_channel(guild)
    except discord.Forbidden:
        print("Не хватает прав для создания канала room-logs")
        return

    if channel is None:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    icon_url = guild_icon_url(guild)
    if icon_url:
        embed.set_author(name=guild.name, icon_url=icon_url)
    for name, value in fields or []:
        embed.add_field(name=name, value=value, inline=True)
    embed.set_footer(text="Журнал системы личных комнат")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print("Не хватает прав для отправки логов в room-logs")


def room_request_embed(guild: discord.Guild, request: dict) -> discord.Embed:
    action = request.get("action")
    payload = request.get("payload", {})
    status = request.get("status", "pending")
    status_labels = {
        "pending": "Ожидает решения",
        "approved": "Одобрено",
        "denied": "Отклонено",
        "failed": "Ошибка выполнения",
    }
    colors = {
        "pending": COLOR_REQUEST_PENDING,
        "approved": COLOR_SETTINGS_OPEN,
        "denied": COLOR_DANGER,
        "failed": discord.Color.dark_red(),
    }

    title = "Заявка на переименование" if action == "rename_channel" else "Заявка на новый канал"
    embed = discord.Embed(
        title=title,
        description=f"Статус: **{status_labels.get(status, status)}**",
        color=colors.get(status, COLOR_REQUEST_PENDING),
        timestamp=parse_utc(request.get("created_at")) or datetime.now(timezone.utc),
    )
    icon_url = guild_icon_url(guild)
    if icon_url:
        embed.set_author(name="Очередь админ-подтверждений", icon_url=icon_url)
    embed.add_field(
        name="Участники",
        value=f"Владелец: {user_mention(request['owner_id'])}\nЗапросил: {user_mention(request['requester_id'])}",
        inline=True,
    )
    embed.add_field(name="Комната", value=channel_label(guild, request.get("category_id")), inline=True)

    if action == "rename_channel":
        embed.add_field(
            name="Детали",
            value=(
                f"Цель: `{payload.get('target', 'не указано')}`\n"
                f"Новое имя: `{payload.get('new_name', 'не указано')}`"
            ),
            inline=True,
        )
    else:
        channel_type = "текстовый" if payload.get("channel_type") == "text" else "голосовой"
        embed.add_field(
            name="Детали",
            value=f"Тип: `{channel_type}`\nНазвание: `{payload.get('name', 'не указано')}`",
            inline=True,
        )

    if request.get("processed_by"):
        embed.add_field(name="Обработал", value=user_mention(request["processed_by"]), inline=True)
    if request.get("result"):
        embed.add_field(name="Результат", value=str(request["result"])[:1024], inline=False)

    footer = f"ID заявки: {request.get('id')}"
    embed.set_footer(text=footer)
    return embed


async def submit_room_request(
    guild: discord.Guild,
    requester: discord.Member | discord.User,
    room: dict,
    action: str,
    payload: dict,
) -> str:
    request_id = uuid.uuid4().hex[:12]
    request = {
        "id": request_id,
        "status": "pending",
        "action": action,
        "payload": payload,
        "owner_id": int(room["owner_id"]),
        "requester_id": int(requester.id),
        "category_id": int(room["category_id"]),
        "created_at": utc_now(),
    }

    channel = await ensure_logs_channel(guild)
    if channel is None:
        raise RuntimeError("канал room-logs не найден")
    message = await channel.send(embed=room_request_embed(guild, request), view=RoomRequestDecisionView(request_id))

    request["channel_id"] = channel.id
    request["message_id"] = message.id
    config = guild_config(guild)
    config["requests"][request_id] = request
    save_config()
    return request_id


def find_request(guild: discord.Guild, request_id: str) -> dict | None:
    return guild_config(guild).get("requests", {}).get(request_id)


def get_request_target_channel(
    guild: discord.Guild,
    room: dict,
    target: str,
) -> discord.abc.GuildChannel | None:
    normalized_target = target.strip().lower()
    category = guild.get_channel(int(room.get("category_id", 0)))

    if normalized_target in {"category", "категория", "room", "комната"}:
        return category if isinstance(category, discord.CategoryChannel) else None

    if normalized_target in {"text", "chat", "чат", "текст"}:
        channel = guild.get_channel(int(room.get("text_channel_id", 0)))
        return channel if isinstance(channel, discord.TextChannel) else None

    if normalized_target in {"voice", "голос", "войс"}:
        channel = guild.get_channel(int(room.get("voice_channel_id", 0)))
        return channel if isinstance(channel, discord.VoiceChannel) else None

    try:
        channel_id = int(normalized_target.strip("<#>"))
    except ValueError:
        return None

    channel = guild.get_channel(channel_id)
    if channel and isinstance(category, discord.CategoryChannel) and channel in category.channels:
        return channel
    return None


async def execute_room_request(guild: discord.Guild, request: dict, moderator: discord.Member | discord.User) -> str:
    room = get_room_by_category(guild, int(request["category_id"]))
    if not room:
        raise RuntimeError("комната не найдена")
    if room.get("archived"):
        raise RuntimeError("комната находится в архиве")

    payload = request.get("payload", {})
    action = request.get("action")

    if action == "rename_channel":
        new_name = sanitize_channel_name(str(payload.get("new_name", "")))
        if not new_name:
            raise RuntimeError("не указано корректное новое имя")

        channel = get_request_target_channel(guild, room, str(payload.get("target", "")))
        if channel is None:
            raise RuntimeError("целевой канал не найден в комнате")

        await channel.edit(name=new_name, reason=f"Одобренная заявка {request['id']} от {moderator}")
        room["updated_at"] = utc_now()
        save_config()
        await update_room_panel_message(guild, room)
        return f"Переименовано в `{new_name}`."

    if action == "create_channel":
        name = sanitize_channel_name(str(payload.get("name", "")))
        channel_type = str(payload.get("channel_type", "")).lower()
        category = guild.get_channel(int(room.get("category_id", 0)))
        if not name:
            raise RuntimeError("не указано корректное название")
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("категория комнаты не найдена")

        if channel_type == "text":
            channel = await guild.create_text_channel(
                name,
                category=category,
                reason=f"Одобренная заявка {request['id']} от {moderator}",
            )
        elif channel_type == "voice":
            channel = await guild.create_voice_channel(
                name,
                category=category,
                user_limit=ROOM_VOICE_USER_LIMIT,
                reason=f"Одобренная заявка {request['id']} от {moderator}",
            )
        else:
            raise RuntimeError("тип канала должен быть `text` или `voice`")

        await channel.edit(
            sync_permissions=True,
            reason=f"Синхронизация прав по заявке {request['id']}",
        )

        extra_channel_ids = normalize_extra_channel_ids(room)
        extra_channel_ids.append(channel.id)
        room["extra_channel_ids"] = extra_channel_ids
        room["updated_at"] = utc_now()
        save_config()
        await update_room_panel_message(guild, room)
        return f"Создан канал {channel.mention if isinstance(channel, discord.TextChannel) else f'`{channel.name}`'}."

    raise RuntimeError("неизвестный тип заявки")


async def update_dashboard_message(guild: discord.Guild) -> None:
    config = guild_config(guild)
    channel = guild.get_channel(int(config.get("dashboard_channel_id") or 0))
    message_id = config.get("dashboard_message_id")

    if not isinstance(channel, discord.TextChannel) or not message_id:
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    await message.edit(embed=dashboard_embed(guild), view=RoomDashboardView())


async def ensure_room_panel(guild: discord.Guild, room: dict) -> discord.Message | None:
    ensure_room_defaults(room)
    if room.get("archived"):
        return None

    text_channel = guild.get_channel(int(room.get("text_channel_id", 0)))
    if not isinstance(text_channel, discord.TextChannel):
        return None

    message = None
    panel_message_id = room.get("panel_message_id")
    if panel_message_id:
        try:
            message = await text_channel.fetch_message(int(panel_message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        message = await text_channel.send(embed=room_panel_embed(guild, room), view=RoomPanelView())
        room["panel_message_id"] = message.id
        save_config()
    else:
        await message.edit(embed=room_panel_embed(guild, room), view=RoomPanelView())

    if not message.pinned:
        try:
            await message.pin(reason="Закрепление панели личной комнаты")
        except discord.HTTPException:
            pass

    return message


async def update_room_panel_message(guild: discord.Guild, room: dict) -> None:
    try:
        await ensure_room_panel(guild, room)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def create_room(guild: discord.Guild, owner: discord.Member) -> dict:
    config = guild_config(guild)
    existing_room = config["rooms"].get(str(owner.id))
    if existing_room and existing_room.get("archived"):
        move_room_to_key(guild, existing_room, archived_room_key(existing_room))

    room_name = safe_room_name(owner)
    category_name = f"room-{room_name}"
    overwrites = await build_room_overwrites(
        guild=guild,
        owner_id=owner.id,
        guest_ids=[],
        status="private",
    )

    category = await guild.create_category(
        category_name,
        overwrites=overwrites,
        reason=f"Создание личной комнаты для {owner}",
    )
    text_channel = await guild.create_text_channel(
        ROOM_TEXT_CHANNEL_NAME,
        category=category,
        reason=f"Создание текстового канала комнаты {owner}",
    )
    voice_channel = await guild.create_voice_channel(
        ROOM_VOICE_CHANNEL_NAME,
        category=category,
        user_limit=ROOM_VOICE_USER_LIMIT,
        reason=f"Создание голосового канала комнаты {owner}",
    )

    room = {
        "owner_id": owner.id,
        "category_id": category.id,
        "text_channel_id": text_channel.id,
        "voice_channel_id": voice_channel.id,
        "status": "private",
        "guest_ids": [],
        "extra_channel_ids": [],
        "archived": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_active_at": utc_now(),
    }
    config["rooms"][str(owner.id)] = room
    save_config()

    await ensure_room_panel(guild, room)
    return room


async def delete_room(guild: discord.Guild, room: dict, reason: str) -> None:
    category = guild.get_channel(int(room["category_id"]))
    channels_to_delete: list[discord.abc.GuildChannel] = []

    if isinstance(category, discord.CategoryChannel):
        channels_to_delete.extend(category.channels)

    channel_ids = [
        room.get("text_channel_id"),
        room.get("voice_channel_id"),
        *normalize_extra_channel_ids(room),
    ]
    for channel_id in channel_ids:
        channel = guild.get_channel(int(channel_id or 0))
        if channel and channel not in channels_to_delete:
            channels_to_delete.append(channel)

    for channel in channels_to_delete:
        try:
            await channel.delete(reason=reason)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

    if isinstance(category, discord.CategoryChannel):
        try:
            await category.delete(reason=reason)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    config = guild_config(guild)
    for key, existing_room in list(config["rooms"].items()):
        if int(existing_room.get("category_id", 0)) == int(room["category_id"]):
            config["rooms"].pop(key, None)
    save_config()


async def archive_room(guild: discord.Guild, room: dict, reason: str) -> None:
    if room.get("archived"):
        return

    category = guild.get_channel(int(room["category_id"]))
    room["archived"] = True
    room["previous_status"] = room.get("status", "private")
    room["archived_at"] = utc_now()
    room["updated_at"] = utc_now()

    if isinstance(category, discord.CategoryChannel):
        new_name = category.name
        if not new_name.startswith(ARCHIVED_ROOM_PREFIX):
            new_name = f"{ARCHIVED_ROOM_PREFIX}{new_name}"[:100]
        await category.edit(name=new_name, reason=reason)

    await apply_room_permissions(guild, room, reason=reason)
    move_room_to_key(guild, room, archived_room_key(room))
    save_config()


async def restore_room(guild: discord.Guild, room: dict, reason: str) -> None:
    if not room.get("archived"):
        return

    category = guild.get_channel(int(room["category_id"]))
    active_room = get_room_for_owner(guild, int(room["owner_id"]))
    if active_room and int(active_room.get("category_id", 0)) != int(room["category_id"]):
        raise RuntimeError("у владельца уже есть активная комната")

    room["archived"] = False
    room["status"] = room.get("previous_status") or "private"
    room["archived_at"] = None
    room["updated_at"] = utc_now()
    room["last_active_at"] = utc_now()

    if isinstance(category, discord.CategoryChannel) and category.name.startswith(ARCHIVED_ROOM_PREFIX):
        new_name = category.name[len(ARCHIVED_ROOM_PREFIX):] or f"room-{room['owner_id']}"
        await category.edit(name=new_name[:100], reason=reason)

    await apply_room_permissions(guild, room, reason=reason)
    move_room_to_key(guild, room, str(room["owner_id"]))
    save_config()


async def require_room_manager(
    interaction: discord.Interaction,
    owner_id: int,
    admin_mode: bool = False,
) -> bool:
    if interaction.user.id == owner_id:
        return True

    if admin_mode and is_room_admin(interaction.user):
        return True

    message = (
        "Админ-панель доступна только участникам с правом `Управление каналами`."
        if admin_mode
        else "Эти настройки доступны только владельцу комнаты."
    )
    await interaction.response.send_message(message, ephemeral=True)
    return False


async def send_interaction_error(interaction: discord.Interaction, error: Exception) -> None:
    print(f"Ошибка interaction: {type(error).__name__}: {error}")
    message = "Что-то пошло не так при обработке кнопки. Ошибка уже выведена в консоль бота."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


class SafeView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await send_interaction_error(interaction, error)


async def require_panel_manager(interaction: discord.Interaction, room: dict) -> bool:
    if interaction.user.id == int(room["owner_id"]) or is_room_admin(interaction.user):
        return True

    await interaction.response.send_message("Эта панель доступна хозяину комнаты и администрации.", ephemeral=True)
    return False


class RoomPanelView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    async def resolve_room(self, interaction: discord.Interaction) -> tuple[str, dict] | tuple[None, None]:
        if not interaction.guild or not isinstance(
            interaction.channel,
            (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel),
        ):
            return None, None
        return get_room_by_channel(interaction.guild, interaction.channel)

    @discord.ui.button(
        label="Запереть/Отомкнуть",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="room_panel:toggle_lock",
    )
    async def toggle_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        room_key, room = await self.resolve_room(interaction)
        if not interaction.guild or not room:
            await interaction.response.send_message("Комната для этой панели не найдена.", ephemeral=True)
            return
        if not await require_panel_manager(interaction, room):
            return
        if room.get("archived"):
            await interaction.response.send_message("Эта комната находится в архиве.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        room["status"] = "open" if room.get("status", "private") == "private" else "private"
        room["updated_at"] = utc_now()
        await apply_room_permissions(
            interaction.guild,
            room,
            reason=f"Переключение режима через панель комнаты пользователем {interaction.user}",
        )
        save_config()
        await update_room_panel_message(interaction.guild, room)
        await update_dashboard_message(interaction.guild)

        status_label = "открыта" if room["status"] == "open" else "заперта"
        await send_room_log(
            interaction.guild,
            title="Режим комнаты изменен",
            description=f"{interaction.user.mention} изменил режим через панель комнаты.",
            color=COLOR_DASHBOARD,
            fields=[
                ("Комната", channel_label(interaction.guild, room.get("category_id"))),
                ("Владелец", user_mention(room["owner_id"])),
                ("Режим", status_label),
            ],
        )
        await interaction.followup.send(f"Готово: комната теперь {status_label}.", ephemeral=True)

    @discord.ui.button(
        label="Гость",
        style=discord.ButtonStyle.success,
        emoji="➕",
        custom_id="room_panel:add_guest",
    )
    async def add_guest(self, interaction: discord.Interaction, button: discord.ui.Button):
        room_key, room = await self.resolve_room(interaction)
        if not interaction.guild or not room:
            await interaction.response.send_message("Комната для этой панели не найдена.", ephemeral=True)
            return
        if not await require_panel_manager(interaction, room):
            return
        if room.get("archived"):
            await interaction.response.send_message("Эта комната находится в архиве.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Выбери гостей для добавления.",
            view=GuestPickerView(
                interaction.guild,
                int(room["owner_id"]),
                mode="add",
                room=room,
                admin_mode=is_room_admin(interaction.user),
                room_key=room_key,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Очистить чат",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        custom_id="room_panel:clear_chat",
    )
    async def clear_chat(self, interaction: discord.Interaction, button: discord.ui.Button):
        room_key, room = await self.resolve_room(interaction)
        if not interaction.guild or not room or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Очистка доступна только в текстовом канале комнаты.", ephemeral=True)
            return
        if int(room.get("text_channel_id", 0)) != interaction.channel.id:
            await interaction.response.send_message("Очищать можно только основной chat комнаты.", ephemeral=True)
            return
        if not await require_panel_manager(interaction, room):
            return

        await interaction.response.send_message(
            "Очистить последние сообщения в этом чате? Закрепленная панель останется на месте.",
            view=ClearChatConfirmView(int(room["owner_id"]), room_key),
            ephemeral=True,
        )


class ClearChatConfirmView(SafeView):
    def __init__(self, owner_id: int, room_key: str | None):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    @discord.ui.button(label="Очистить", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Очистка доступна только в текстовом канале комнаты.", ephemeral=True)
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", view=None)
            return
        if not await require_panel_manager(interaction, room):
            return

        panel_message_id = int(room.get("panel_message_id", 0) or 0)

        def can_delete(message: discord.Message) -> bool:
            return not message.pinned and message.id != panel_message_id

        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(limit=100, check=can_delete, reason=f"Очистка чата комнаты пользователем {interaction.user}")
        await ensure_room_panel(interaction.guild, room)
        await send_room_log(
            interaction.guild,
            title="Чат комнаты очищен",
            description=f"{interaction.user.mention} очистил сообщения через панель комнаты.",
            color=COLOR_REQUEST_PENDING,
            fields=[
                ("Комната", channel_label(interaction.guild, room.get("category_id"))),
                ("Владелец", user_mention(room["owner_id"])),
                ("Удалено", str(len(deleted))),
            ],
        )
        await interaction.followup.send(f"Удалено сообщений: `{len(deleted)}`.", ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Очистка отменена.", view=None)


class RoomDashboardView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Создать комнату",
        style=discord.ButtonStyle.success,
        emoji="✨",
        custom_id="rooms:create",
        row=0,
    )
    async def create_room_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        existing_room = get_room_for_owner(interaction.guild, interaction.user.id)
        if existing_room:
            await interaction.response.send_message(
                "🏡 У тебя уже есть активная комната.",
                embed=settings_embed(interaction.guild, existing_room),
                view=RoomSettingsView(interaction.user.id),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            room = await create_room(interaction.guild, interaction.user)
        except discord.Forbidden:
            await interaction.followup.send("Мне не хватает прав на создание категорий и каналов.", ephemeral=True)
            return

        await send_room_log(
            interaction.guild,
            title="Комната создана",
            description=f"{interaction.user.mention} создал личную комнату.",
            color=COLOR_SETTINGS_OPEN,
            fields=[
                ("Владелец", interaction.user.mention),
                ("Категория", channel_label(interaction.guild, room["category_id"])),
                ("Статус", "Приватная"),
            ],
        )
        await update_dashboard_message(interaction.guild)
        await interaction.followup.send(
            "✅ Готово! Твоя комната уже ждёт гостей.",
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Настройки",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="rooms:settings",
        row=0,
    )
    async def settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        room = get_room_for_owner(interaction.guild, interaction.user.id)
        if not room:
            await interaction.response.send_message("🏡 Сначала создай комнату на главном дэшборде.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Админ-панель",
        style=discord.ButtonStyle.secondary,
        emoji="🗝️",
        custom_id="rooms:admin",
        row=0,
    )
    async def admin_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not is_room_admin(interaction.user):
            await interaction.response.send_message(
                "Админ-панель доступна только участникам с правом `Управление каналами`.",
                ephemeral=True,
            )
            return

        rooms = get_all_rooms(interaction.guild)
        if not rooms:
            await interaction.response.send_message(embed=admin_panel_embed(interaction.guild), ephemeral=True)
            return

        await interaction.response.send_message(
            embed=admin_panel_embed(interaction.guild),
            view=AdminRoomsView(interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Удалить комнату",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        custom_id="rooms:delete",
        row=1,
    )
    async def delete_room_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return

        room = get_room_for_owner(interaction.guild, interaction.user.id)
        if not room:
            await interaction.response.send_message("У тебя пока нет активной комнаты для удаления.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Удалить твою комнату вместе со всеми каналами внутри?",
            view=DeleteRoomConfirmView(interaction.user.id),
            ephemeral=True,
        )


class AdminRoomsView(SafeView):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.add_item(AdminRoomSelect(guild))


class AdminRoomSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild):
        options = []

        for room in get_all_rooms(guild)[:25]:
            room_key = find_room_key(guild, room) or str(room["owner_id"])
            owner_id = int(room["owner_id"])
            owner = guild.get_member(owner_id)
            owner_name = owner.display_name if owner else str(owner_id)
            if room.get("archived"):
                status = "архив"
            else:
                status = "открытая" if room.get("status") == "open" else "приватная"
            guests_count = len(normalize_guest_ids(room))
            options.append(
                discord.SelectOption(
                    label=owner_name[:100],
                    value=room_key[:100],
                    description=f"{status}, гостей: {guests_count}",
                )
            )

        super().__init__(
            placeholder="Выбери комнату",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Этот выбор работает только на сервере.", ephemeral=True)
            return
        if not is_room_admin(interaction.user):
            await interaction.response.send_message(
                "Админ-панель доступна только участникам с правом `Управление каналами`.",
                ephemeral=True,
            )
            return

        room_key = self.values[0]
        room = get_room_by_key(interaction.guild, room_key)
        if not room:
            await interaction.response.edit_message(
                content="Комната не найдена. Список комнат был обновлен.",
                embed=admin_panel_embed(interaction.guild),
                view=AdminRoomsView(interaction.guild) if get_all_rooms(interaction.guild) else None,
            )
            return

        owner_id = int(room["owner_id"])
        await interaction.response.edit_message(
            content=None,
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(owner_id, admin_mode=True, room_key=room_key),
        )


class RoomRequestDecisionView(SafeView):
    def __init__(self, request_id: str):
        super().__init__(timeout=None)
        self.request_id = request_id

        approve_button = discord.ui.Button(
            label="Да",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"room_request:{request_id}:approve",
        )
        deny_button = discord.ui.Button(
            label="Нет",
            style=discord.ButtonStyle.danger,
            emoji="✖️",
            custom_id=f"room_request:{request_id}:deny",
        )
        approve_button.callback = self.approve
        deny_button.callback = self.deny
        self.add_item(approve_button)
        self.add_item(deny_button)

    async def approve(self, interaction: discord.Interaction):
        await self.process(interaction, approved=True)

    async def deny(self, interaction: discord.Interaction):
        await self.process(interaction, approved=False)

    async def process(self, interaction: discord.Interaction, approved: bool):
        if not interaction.guild:
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return
        if not is_room_admin(interaction.user):
            await interaction.response.send_message("Нужно право `Управление каналами`.", ephemeral=True)
            return

        request = find_request(interaction.guild, self.request_id)
        if not request:
            await interaction.response.send_message("Заявка не найдена.", ephemeral=True)
            return
        if request.get("status") != "pending":
            await interaction.response.send_message("Эта заявка уже обработана.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        request["processed_by"] = interaction.user.id
        request["processed_at"] = utc_now()

        if approved:
            try:
                request["result"] = await execute_room_request(interaction.guild, request, interaction.user)
                request["status"] = "approved"
            except (discord.Forbidden, discord.HTTPException, RuntimeError) as error:
                request["result"] = str(error)
                request["status"] = "failed"
        else:
            request["result"] = "Заявка отклонена."
            request["status"] = "denied"

        save_config()
        await update_dashboard_message(interaction.guild)

        try:
            await interaction.message.edit(
                embed=room_request_embed(interaction.guild, request),
                view=None,
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(request["result"], ephemeral=True)


class DeleteRoomConfirmView(SafeView):
    def __init__(self, owner_id: int, admin_mode: bool = False, room_key: str | None = None):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    @discord.ui.button(label="Подтвердить удаление", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната уже удалена.", view=None)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await delete_room(
            interaction.guild,
            room,
            reason=f"Удаление личной комнаты пользователем {interaction.user}",
        )
        await send_room_log(
            interaction.guild,
            title="Комната удалена",
            description=f"{interaction.user.mention} удалил личную комнату.",
            color=COLOR_DANGER,
            fields=[
                ("Владелец", user_mention(self.owner_id)),
                ("Инициатор", interaction.user.mention),
            ],
        )
        await update_dashboard_message(interaction.guild)
        await interaction.followup.send("Комната удалена.", ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return
        await interaction.response.edit_message(content="Удаление отменено.", view=None)


class RoomSettingsView(SafeView):
    def __init__(self, owner_id: int, admin_mode: bool = False, room_key: str | None = None):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key
        if not self.admin_mode:
            for item in list(self.children):
                if isinstance(item, discord.ui.Button) and item.label == "Архив/вернуть":
                    self.remove_item(item)

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    def fresh_view(self) -> "RoomSettingsView":
        return RoomSettingsView(self.owner_id, self.admin_mode, self.room_key)

    @discord.ui.button(label="Режим доступа", style=discord.ButtonStyle.secondary, emoji="🔐", row=0)
    async def toggle_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", embed=None, view=None)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        await interaction.response.defer()
        room["status"] = "open" if room.get("status", "private") == "private" else "private"
        room["updated_at"] = utc_now()
        await apply_room_permissions(
            interaction.guild,
            room,
            reason=f"Изменение статуса личной комнаты пользователем {interaction.user}",
        )
        save_config()

        status_label = "Открытая" if room["status"] == "open" else "Приватная"
        await send_room_log(
            interaction.guild,
            title="Статус комнаты изменен",
            description=f"{interaction.user.mention} изменил статус комнаты.",
            color=COLOR_DASHBOARD,
            fields=[
                ("Владелец", user_mention(self.owner_id)),
                ("Инициатор", interaction.user.mention),
                ("Статус", status_label),
            ],
        )
        await update_room_panel_message(interaction.guild, room)
        await interaction.edit_original_response(
            embed=settings_embed(interaction.guild, room),
            view=self.fresh_view(),
        )

    @discord.ui.button(label="Добавить гостя", style=discord.ButtonStyle.success, emoji="➕", row=1)
    async def add_guest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Выбери гостей для добавления.",
            view=GuestPickerView(
                interaction.guild,
                self.owner_id,
                mode="add",
                room=room,
                admin_mode=self.admin_mode,
                room_key=self.room_key,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Удалить гостя", style=discord.ButtonStyle.secondary, emoji="➖", row=1)
    async def remove_guest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        if not normalize_guest_ids(room):
            await interaction.response.send_message("В списке гостей пока никого нет.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Выбери гостей для удаления.",
            view=GuestPickerView(
                interaction.guild,
                self.owner_id,
                mode="remove",
                room=room,
                admin_mode=self.admin_mode,
                room_key=self.room_key,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Переименовать канал", style=discord.ButtonStyle.secondary, emoji="✏️", row=2)
    async def request_rename_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        await interaction.response.send_modal(RenameChannelRequestModal(self.owner_id, self.admin_mode, self.room_key))

    @discord.ui.button(label="Создать канал", style=discord.ButtonStyle.secondary, emoji="🧩", row=2)
    async def request_create_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=channel_type_choice_embed(),
            view=ChannelTypeChoiceView(self.owner_id, self.admin_mode, self.room_key),
            ephemeral=True,
        )

    @discord.ui.button(label="Архив/вернуть", style=discord.ButtonStyle.secondary, emoji="📦", row=3)
    async def toggle_archive(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not self.admin_mode or not is_room_admin(interaction.user):
            await interaction.response.send_message(
                "Архивировать и восстанавливать комнаты может только администрация.",
                ephemeral=True,
            )
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", embed=None, view=None)
            return

        await interaction.response.defer()
        if room.get("archived"):
            try:
                await restore_room(interaction.guild, room, reason=f"Восстановление комнаты пользователем {interaction.user}")
            except RuntimeError as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            title = "Комната восстановлена"
            description = f"{interaction.user.mention} восстановил комнату из архива."
            color = COLOR_SETTINGS_OPEN
        else:
            await archive_room(interaction.guild, room, reason=f"Архивация комнаты пользователем {interaction.user}")
            title = "Комната архивирована"
            description = f"{interaction.user.mention} отправил комнату в архив."
            color = COLOR_REQUEST_PENDING

        await send_room_log(
            interaction.guild,
            title=title,
            description=description,
            color=color,
            fields=[
                ("Владелец", user_mention(self.owner_id)),
                ("Инициатор", interaction.user.mention),
            ],
        )
        await update_dashboard_message(interaction.guild)
        await update_room_panel_message(interaction.guild, room)
        next_room_key = find_room_key(interaction.guild, room) if self.admin_mode else self.room_key
        await interaction.edit_original_response(
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(self.owner_id, self.admin_mode, next_room_key),
        )

    @discord.ui.button(label="Удалить комнату", style=discord.ButtonStyle.danger, emoji="🗑️", row=3)
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        await interaction.response.send_message(
            "Удалить комнату вместе со всеми каналами внутри?",
            view=DeleteRoomConfirmView(self.owner_id, self.admin_mode, self.room_key),
            ephemeral=True,
        )

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", embed=None, view=None)
            return

        await interaction.response.edit_message(
            embed=settings_embed(interaction.guild, room),
            view=self.fresh_view(),
        )


class RenameChannelRequestModal(discord.ui.Modal, title="Запрос переименования"):
    target = discord.ui.TextInput(
        label="Что переименовать",
        placeholder="category, chat, voice или ID канала",
        max_length=100,
    )
    new_name = discord.ui.TextInput(
        label="Новое название",
        placeholder="Например: music-room",
        max_length=90,
    )

    def __init__(self, owner_id: int, admin_mode: bool = False, room_key: str | None = None):
        super().__init__()
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_interaction_error(interaction, error)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Форма работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        safe_name = sanitize_channel_name(str(self.new_name.value))
        if not safe_name:
            await interaction.response.send_message("Название канала не может быть пустым.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            request_id = await submit_room_request(
                interaction.guild,
                interaction.user,
                room,
                action="rename_channel",
                payload={
                    "target": str(self.target.value).strip(),
                    "new_name": safe_name,
                },
            )
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as error:
            await interaction.followup.send(f"Не удалось отправить заявку: {error}", ephemeral=True)
            return

        await interaction.followup.send(f"Заявка `{request_id}` отправлена администраторам.", ephemeral=True)


def channel_type_choice_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Создание канала",
        description="Выбери тип канала, а затем введи только название. Заявка уйдет администраторам на подтверждение.",
        color=COLOR_DASHBOARD,
    )
    embed.set_footer(text="Так меньше ошибок с телефона и быстрее для владельца комнаты.")
    return embed


class ChannelTypeChoiceView(SafeView):
    def __init__(self, owner_id: int, admin_mode: bool = False, room_key: str | None = None):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    async def open_modal(self, interaction: discord.Interaction, channel_type: str):
        if not interaction.guild:
            await interaction.response.send_message("Эта кнопка работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        await interaction.response.send_modal(
            CreateChannelRequestModal(self.owner_id, channel_type, self.admin_mode, self.room_key)
        )

    @discord.ui.button(label="Текстовый", style=discord.ButtonStyle.secondary, emoji="💬")
    async def text_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_modal(interaction, "text")

    @discord.ui.button(label="Голосовой", style=discord.ButtonStyle.secondary, emoji="🔊")
    async def voice_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_modal(interaction, "voice")


class CreateChannelRequestModal(discord.ui.Modal, title="Запрос нового канала"):
    name = discord.ui.TextInput(
        label="Название",
        placeholder="Например: clips",
        max_length=90,
    )

    def __init__(self, owner_id: int, channel_type: str, admin_mode: bool = False, room_key: str | None = None):
        super().__init__()
        self.owner_id = owner_id
        self.channel_type = channel_type
        self.admin_mode = admin_mode
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_interaction_error(interaction, error)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Форма работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.send_message("Комната не найдена.", ephemeral=True)
            return
        if room.get("archived"):
            await interaction.response.send_message("Сначала восстанови комнату из архива.", ephemeral=True)
            return

        safe_name = sanitize_channel_name(str(self.name.value))
        if not safe_name:
            await interaction.response.send_message("Название канала не может быть пустым.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            request_id = await submit_room_request(
                interaction.guild,
                interaction.user,
                room,
                action="create_channel",
                payload={
                    "channel_type": self.channel_type,
                    "name": safe_name,
                },
            )
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as error:
            await interaction.followup.send(f"Не удалось отправить заявку: {error}", ephemeral=True)
            return

        await interaction.followup.send(f"Заявка `{request_id}` отправлена администраторам.", ephemeral=True)


class GuestPickerView(SafeView):
    def __init__(
        self,
        guild: discord.Guild,
        owner_id: int,
        mode: str,
        room: dict,
        admin_mode: bool = False,
        room_key: str | None = None,
    ):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.mode = mode
        self.admin_mode = admin_mode
        self.room_key = room_key

        if mode == "add":
            self.add_item(AddGuestSelect(owner_id, admin_mode, room_key))
        else:
            self.add_item(RemoveGuestSelect(guild, owner_id, room, admin_mode, room_key))


class AddGuestSelect(discord.ui.UserSelect):
    def __init__(self, owner_id: int, admin_mode: bool = False, room_key: str | None = None):
        super().__init__(
            placeholder="Выбери пользователей",
            min_values=1,
            max_values=5,
        )
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Этот выбор работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", view=None)
            return

        await interaction.response.defer()
        guest_ids = normalize_guest_ids(room)
        added_mentions = []

        for selected_user in self.values:
            member = await fetch_member(interaction.guild, selected_user.id)
            if member is None or member.bot or member.id == self.owner_id:
                continue

            if member.id not in guest_ids:
                guest_ids.append(member.id)
                added_mentions.append(member.mention)

        room["guest_ids"] = guest_ids
        room["updated_at"] = utc_now()

        if added_mentions:
            await apply_room_permissions(
                interaction.guild,
                room,
                reason=f"Добавление гостей пользователем {interaction.user}",
            )
            save_config()
            await update_room_panel_message(interaction.guild, room)
            await send_room_log(
                interaction.guild,
                title="Гости добавлены",
                description=f"{interaction.user.mention} добавил гостей в комнату.",
                color=COLOR_SETTINGS_OPEN,
                fields=[
                    ("Владелец", user_mention(self.owner_id)),
                    ("Инициатор", interaction.user.mention),
                    ("Гости", ", ".join(added_mentions)),
                ],
            )
            result = f"Добавлены: {', '.join(added_mentions)}"
        else:
            result = "Новых гостей для добавления не выбрано."

        await interaction.edit_original_response(content=result, view=None)
        await interaction.followup.send(
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(self.owner_id, self.admin_mode, self.room_key),
            ephemeral=True,
        )


class RemoveGuestSelect(discord.ui.Select):
    def __init__(
        self,
        guild: discord.Guild,
        owner_id: int,
        room: dict,
        admin_mode: bool = False,
        room_key: str | None = None,
    ):
        self.owner_id = owner_id
        self.admin_mode = admin_mode
        self.room_key = room_key
        options = []

        for guest_id in normalize_guest_ids(room)[:25]:
            member = guild.get_member(guest_id)
            label = member.display_name if member else str(guest_id)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(guest_id),
                    description=f"ID: {guest_id}",
                )
            )

        super().__init__(
            placeholder="Выбери гостей",
            min_values=1,
            max_values=max(1, min(5, len(options))),
            options=options,
        )

    def resolve_room(self, guild: discord.Guild) -> dict | None:
        if self.room_key:
            return get_room_by_key(guild, self.room_key)
        return get_room_for_owner(guild, self.owner_id)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Этот выбор работает только на сервере.", ephemeral=True)
            return
        if not await require_room_manager(interaction, self.owner_id, self.admin_mode):
            return

        room = self.resolve_room(interaction.guild)
        if not room:
            await interaction.response.edit_message(content="Комната не найдена.", view=None)
            return

        await interaction.response.defer()
        selected_ids = {int(value) for value in self.values}
        guest_ids = normalize_guest_ids(room)
        removed_ids = [guest_id for guest_id in guest_ids if guest_id in selected_ids]
        room["guest_ids"] = [guest_id for guest_id in guest_ids if guest_id not in selected_ids]
        room["updated_at"] = utc_now()

        if removed_ids:
            await apply_room_permissions(
                interaction.guild,
                room,
                reason=f"Удаление гостей пользователем {interaction.user}",
            )
            save_config()
            await update_room_panel_message(interaction.guild, room)
            await send_room_log(
                interaction.guild,
                title="Гости удалены",
                description=f"{interaction.user.mention} убрал гостей из комнаты.",
                color=COLOR_REQUEST_PENDING,
                fields=[
                    ("Владелец", user_mention(self.owner_id)),
                    ("Инициатор", interaction.user.mention),
                    ("Гости", ", ".join(user_mention(guest_id) for guest_id in removed_ids)),
                ],
            )
            result = "Удалены: " + ", ".join(user_mention(guest_id) for guest_id in removed_ids)
        else:
            result = "Никто не был удален."

        await interaction.edit_original_response(content=result, view=None)
        await interaction.followup.send(
            embed=settings_embed(interaction.guild, room),
            view=RoomSettingsView(self.owner_id, self.admin_mode, self.room_key),
            ephemeral=True,
        )


async def sync_configured_rooms(guild: discord.Guild) -> None:
    for room in get_all_rooms(guild):
        try:
            await apply_room_permissions(guild, room, reason="Синхронизация прав личных комнат")
            await ensure_room_panel(guild, room)
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            continue


def room_has_voice_members(guild: discord.Guild, room: dict) -> bool:
    voice_channel = guild.get_channel(int(room.get("voice_channel_id", 0)))
    return isinstance(voice_channel, discord.VoiceChannel) and bool(voice_channel.members)


async def mark_room_activity_for_channel(channel: discord.abc.GuildChannel | None) -> None:
    if not isinstance(channel, discord.VoiceChannel):
        return

    config = guild_config(channel.guild)
    for room in config["rooms"].values():
        if int(room.get("voice_channel_id", 0)) == channel.id:
            ensure_room_defaults(room)
            room["last_active_at"] = utc_now()
            save_config()
            return


async def run_room_maintenance(guild: discord.Guild) -> None:
    now = datetime.now(timezone.utc)
    changed = False

    for room in list(get_all_rooms(guild)):
        ensure_room_defaults(room)

        if room.get("archived"):
            archived_at = parse_utc(room.get("archived_at"))
            if archived_at and now - archived_at >= AUTO_DELETE_ARCHIVED_AFTER:
                owner_id = int(room["owner_id"])
                await delete_room(
                    guild,
                    room,
                    reason="Автоудаление старой архивной комнаты",
                )
                await send_room_log(
                    guild,
                    title="Архивная комната удалена",
                    description="Бот удалил комнату после срока хранения в архиве.",
                    color=COLOR_DANGER,
                    fields=[("Владелец", user_mention(owner_id))],
                )
                changed = True
            continue

        if room_has_voice_members(guild, room):
            room["last_active_at"] = utc_now()
            changed = True
            continue

        last_active_at = parse_utc(room.get("last_active_at")) or parse_utc(room.get("updated_at"))
        if last_active_at and now - last_active_at >= AUTO_ARCHIVE_AFTER:
            await archive_room(
                guild,
                room,
                reason="Автоархивация пустующей комнаты",
            )
            await send_room_log(
                guild,
                title="Комната архивирована автоматически",
                description="Бот отправил пустующую комнату в архив.",
                color=COLOR_REQUEST_PENDING,
                fields=[("Владелец", user_mention(room["owner_id"]))],
            )
            changed = True

    if changed:
        save_config()
        await update_dashboard_message(guild)


@tasks.loop(minutes=ROOM_MAINTENANCE_INTERVAL_MINUTES)
async def room_maintenance():
    for guild in bot.guilds:
        await run_room_maintenance(guild)


@room_maintenance.before_loop
async def before_room_maintenance():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен и готов к работе!")
    for guild in bot.guilds:
        await sync_configured_rooms(guild)
        await update_dashboard_message(guild)


@bot.event
async def on_voice_state_update(member, before, after):
    await mark_room_activity_for_channel(before.channel)
    await mark_room_activity_for_channel(after.channel)


@bot.tree.command(name="setup_rooms", description="Создать или обновить дэшборд личных комнат")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(channel="Канал для дэшборда. Если не выбрать, бот создаст или найдет room-dashboard.")
async def setup_rooms(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not interaction.guild:
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    target_channel = channel or discord.utils.get(interaction.guild.text_channels, name=DASHBOARD_CHANNEL_NAME)
    if target_channel is None:
        target_channel = await interaction.guild.create_text_channel(
            DASHBOARD_CHANNEL_NAME,
            reason=f"Создание дэшборда личных комнат пользователем {interaction.user}",
        )

    logs_warning = None
    try:
        await ensure_logs_channel(interaction.guild)
    except discord.Forbidden:
        logs_warning = " Мне не хватило прав создать `room-logs`."

    config = guild_config(interaction.guild)
    config["dashboard_channel_id"] = target_channel.id

    message = None
    message_id = config.get("dashboard_message_id")
    if message_id:
        try:
            message = await target_channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        message = await target_channel.send(embed=dashboard_embed(interaction.guild), view=RoomDashboardView())
    else:
        await message.edit(embed=dashboard_embed(interaction.guild), view=RoomDashboardView())

    config["dashboard_message_id"] = message.id
    save_config()

    if logs_warning is None:
        await send_room_log(
            interaction.guild,
            title="Дэшборд обновлен",
            description=f"{interaction.user.mention} обновил дэшборд личных комнат.",
            color=COLOR_DASHBOARD,
            fields=[("Канал", target_channel.mention)],
        )

    await interaction.followup.send(f"Дэшборд готов: {target_channel.mention}{logs_warning or ''}", ephemeral=True)


@setup_rooms.error
async def setup_rooms_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Нужны права `Управление каналами`.", ephemeral=True)


@bot.tree.command(name="settings", description="Открыть настройки своей комнаты")
@app_commands.guild_only()
async def settings(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return

    room = get_room_for_owner(interaction.guild, interaction.user.id)
    if not room:
        await interaction.response.send_message("У тебя пока нет комнаты. Создай её через дэшборд.", ephemeral=True)
        return

    await interaction.response.send_message(
        embed=settings_embed(interaction.guild, room),
        view=RoomSettingsView(interaction.user.id),
        ephemeral=True,
    )


if __name__ == "__main__":
    load_env_file()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Не найден DISCORD_TOKEN. Укажите токен бота в .env или переменной окружения.")

    bot.run(token)
