import discord
from discord.ext import commands

# === ВЫПАДАЮЩЕЕ МЕНЮ ДЛЯ ВЫБОРА ТИПА АКТИВНОСТИ ===
class ActivityTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Играет в...", value="playing", description="Отображает: Играет в [текст]", emoji="🎮"),
            discord.SelectOption(label="Смотрит...", value="watching", description="Отображает: Смотрит [текст]", emoji="📺"),
            discord.SelectOption(label="Слушает...", value="listening", description="Отображает: Слушает [текст]", emoji="🎧"),
            discord.SelectOption(label="Стримит...", value="streaming", description="Отображает: Стримит [текст]", emoji="💜"),
        ]
        super().__init__(placeholder="Выбери тип активности...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        # Сохраняем выбранный тип активности прямо во view, чтобы использовать позже
        self.view.selected_activity_type = self.values[0]
        await interaction.response.send_message(
            f"Выбран тип: **{self.values[0]}**. Теперь нажми кнопку «Изменить текст» или выбери статус.", 
            ephemeral=True
        )


# === МОДАЛЬНОЕ ОКНО (ОКНО ВВОДА) ДЛЯ ТЕКСТА СТАТУСА ===
class ChangeTextModal(discord.ui.Modal, title="Изменение описания активности"):
    status_text = discord.ui.TextInput(
        label="Что должен делать бот?", 
        placeholder="Например: за порядком / Python / музыку", 
        required=True, 
        max_length=128
    )

    def __init__(self, view_instance):
        super().__init__()
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        text = self.status_text.value
        self.view_instance.current_text = text
        
        # Обновляем статус бота
        await self.view_instance.update_bot_presence(interaction.client)
        
        # Обновляем сообщение дашборда
        await interaction.response.edit_message(embed=self.view_instance.create_embed(interaction.client), view=self.view_instance)


# === ИНТЕРФЕЙС ПАНЕЛИ УПРАВЛЕНИЯ ===
class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # Панель будет работать "вечно", пока бот не перезагрузится
        self.selected_activity_type = "playing" # По умолчанию "Играет"
        self.current_text = "Логи сервера"       # Начальный текст по умолчанию
        
        # Добавляем выпадающее меню
        self.add_item(ActivityTypeSelect())

    def create_embed(self, bot: commands.Bot) -> discord.Embed:
        """Вспомогательный метод для генерации красивого статуса в эмбед"""
        status_map = {
            discord.Status.online: "🟢 В сети (Online)",
            discord.Status.idle: "🟡 Не активен (Idle)",
            discord.Status.dnd: "🔴 Не беспокоить (DND)",
            discord.Status.invisible: "⚫ Невидимка (Invisible)"
        }
        
        # Находим текущий статус на основе данных первого сервера (т.к. у бота глобальный статус)
        current_status = discord.Status.online
        if bot.guilds:
            me = bot.guilds[0].me
            if me:
                current_status = me.status

        embed = discord.Embed(
            title="🎛️ Панель управления статусом бота",
            description="Здесь вы можете изменить цвет значка (статус) и текст активности бота.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Текущий значок", value=status_map.get(current_status, "🟢 В сети"), inline=False)
        embed.add_field(name="Тип активности в меню", value=f"`{self.selected_activity_type}`", inline=True)
        embed.add_field(name="Текст активности", value=f"`{self.current_text}`", inline=True)
        embed.set_footer(text="Доступно только владельцу бота")
        return embed

    async def update_bot_presence(self, bot: commands.Bot, new_status: discord.Status = None):
        """Метод, который физически меняет присутствие бота в Discord"""
        # Если статус не передан, берем текущий статус бота на сервере
        if new_status is None and bot.guilds:
            new_status = bot.guilds[0].me.status
        if new_status is None:
            new_status = discord.Status.online

        # Формируем объект активности
        activity = None
        if self.selected_activity_type == "playing":
            activity = discord.Game(name=self.current_text)
        elif self.selected_activity_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=self.current_text)
        elif self.selected_activity_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=self.current_text)
        elif self.selected_activity_type == "streaming":
            # Для стрима нужна ссылка, укажем стандартную заглушку на Twitch
            activity = discord.Streaming(name=self.current_text, url="https://twitch.tv/discord")

        await bot.change_presence(status=new_status, activity=activity)

    # Кнопки изменения цвета кружка (Статуса)
    @discord.ui.button(label="В сети", style=discord.ButtonStyle.success, emoji="🟢", row=2)
    async def set_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bot_presence(interaction.client, discord.Status.online)
        await interaction.response.edit_message(embed=self.create_embed(interaction.client), view=self)

    @discord.ui.button(label="Не активен", style=discord.ButtonStyle.secondary, emoji="🟡", row=2)
    async def set_idle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bot_presence(interaction.client, discord.Status.idle)
        await interaction.response.edit_message(embed=self.create_embed(interaction.client), view=self)

    @discord.ui.button(label="Не беспокоить", style=discord.ButtonStyle.danger, emoji="🔴", row=2)
    async def set_dnd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bot_presence(interaction.client, discord.Status.dnd)
        await interaction.response.edit_message(embed=self.create_embed(interaction.client), view=self)

    @discord.ui.button(label="Невидимка", style=discord.ButtonStyle.primary, emoji="⚫", row=2)
    async def set_invisible(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bot_presence(interaction.client, discord.Status.invisible)
        await interaction.response.edit_message(embed=self.create_embed(interaction.client), view=self)

    # Кнопка вызова модального окна для ввода текста
    @discord.ui.button(label="Изменить текст описания", style=discord.ButtonStyle.primary, emoji="✏️", row=3)
    async def change_text_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChangeTextModal(self))


# === ПОДКЛЮЧЕНИЕ КОГ-МОДУЛЯ ===
class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="status_db", aliases=["dashboard", "панель"])
    @commands.is_owner() # Жесткая проверка: команду сможет вызвать ТОЛЬКО владелец приложения
    async def status_dashboard(self, ctx: commands.Context):
        view = DashboardView()
        embed = view.create_embed(self.bot)
        await ctx.send(embed=embed, view=view)

    @status_dashboard.error
    async def dashboard_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ Эта команда доступна только создателю/хозяину бота!", delete_after=5)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))