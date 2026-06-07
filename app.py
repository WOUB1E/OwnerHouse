import os
from pathlib import Path

import discord
from discord.ext import commands

from Activity import setup as setup_dashboard

from Economy import setup as setup_economy
from Logs import setup as setup_logs
from Lvl import setup as setup_levels

# Комнаты вынесены в Rooms.py и намеренно не подключены.
# import Rooms


ENV_PATH = Path(__file__).with_name(".env")


intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.presences = True
intents.message_content = True
intents.moderation = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await setup_logs(self)
        await setup_levels(self)
        await setup_economy(self)
        await setup_dashboard(self)
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


@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен и готов к работе.")




if __name__ == "__main__":
    load_env_file()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Не найден DISCORD_TOKEN. Укажи токен в .env или переменной окружения.")

    bot.run(token)
