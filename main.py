"""
Arizona Mesa — бот мероприятий (discord.py 2.x)
Игры: Калькулятор, Угадайка, Turbo, Угадай Вещь

ENV:
  DISCORD_TOKEN   — токен бота (обязательно)
  GUILD_ID        — ID сервера (быстрая синхронизация команд, желательно)
  HOST_ROLE_ID    — роль ведущих (у кого есть Manage Messages — тоже может)
  MP_ROLE_ID      — роль «Участник МП» (пингуется в анонсе)

В Developer Portal включи Message Content Intent!
"""
import asyncio
import difflib
import os
import random
import re
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1554517494474088469")) or None
HOST_ROLE_ID = int(os.getenv("HOST_ROLE_ID", "1554517494499123210")) or None
MP_ROLE_ID = int(os.getenv("MP_ROLE_ID", "1555282937639870514")) or None

# ───────────────────────── Описание игр ─────────────────────────
KINDS = {
    "calc": dict(
        title="Калькулятор", emoji="✅", rounds=5, minutes=None,
        task='Ваша задача решить математический пример, написанный ведущим. '
             'Побеждает тот, кто решит быстрее всех 5 примеров!',
        rules="При такой ❌ - неверно\nПри такой ✅ - верно (+ балл)",
        outro="Желаю удачи всем участникам!",
    ),
    "guess": dict(
        title="Угадайка", emoji="✅", rounds=5, minutes=None,
        task='Ваша задача отгадать загаданный предмет из выбранной темы. '
             'Побеждает тот, кто отгадает быстрее всех 5 предметов!',
        rules="При такой ❌ - неверно\nПри такой ✅ - верно (+ балл)",
        outro="Желаю удачи всем участникам!",
    ),
    "turbo": dict(
        title="Turbo", emoji="💍", rounds=None, minutes=20,
        task='Ваша главная цель - отгадать автомобиль, указав полное название авто и гос цену.\n\n'
             'Суть игры: Ведущий прикрепляет фотографию автомобиля, а вы должны первым назвать '
             'полное название автомобиля и его гос стоимость.',
        rules="При такой ❌ - неверно\nПри такой 💍 - победитель",
        outro="Игра будет длится 20 минут! Желаю удачи всем участникам!",
    ),
    "thing": dict(
        title="Угадай Вещь", emoji="💍", rounds=None, minutes=20,
        task='Ваша задача угадывать замазанную вещь на фото, по реакции от организатора мероприятий!',
        rules="При такой ✅ - близко\nПри такой ❌ - далеко\nПри такой 💍 - победитель",
        outro="Игра будет длится 20 минут! Желаю удачи всем участникам!",
    ),
}


def announcement(kind: str, prize: str) -> str:
    k = KINDS[kind]
    ping = f"<@&{MP_ROLE_ID}>\n" if MP_ROLE_ID else ""
    return (
        f"{ping}⚔️ Доброго времени суток, Уважаемые пользователи Дискорда Arizona Mesa!⚔️\n"
        f'Сейчас пройдёт мероприятие название которого "{k["title"]}"\n'
        f"{k['task']}\n\n"
        f"Призовой фонд мероприятия: {prize}👑\n"
        f"{k['rules']}\n\n"
        f"🎤 {k['outro']}"
    )


# ───────────────────────── Состояние ─────────────────────────
@dataclass
class Game:
    kind: str
    host_id: int
    channel: discord.abc.Messageable
    prize: str
    max_rounds: Optional[int]
    round_no: int = 0
    open: bool = False
    finished: bool = False
    answer: str = ""
    price: Optional[int] = None
    scores: dict = field(default_factory=dict)
    panel: Optional[discord.Message] = None
    panel_view: Optional[discord.ui.View] = None
    timer: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


GAMES: dict[int, Game] = {}  # channel_id -> Game


def is_host(g: Game, user: discord.abc.User) -> bool:
    if user.id == g.host_id:
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_messages)


def can_start(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    if perms and perms.manage_messages:
        return True
    return bool(HOST_ROLE_ID and any(r.id == HOST_ROLE_ID for r in getattr(user, "roles", [])))


# ───────────────────────── Проверка ответов ─────────────────────────
def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я ]+", " ", s).split())


def match_text(answer: str, text: str) -> bool:
    nt = norm(text)
    for variant in answer.split("|"):
        nv = norm(variant)
        if not nv:
            continue
        if f" {nv} " in f" {nt} ":
            return True
        if difflib.SequenceMatcher(None, nv, nt).ratio() >= 0.85:
            return True
    return False


def parse_prices(text: str) -> set[int]:
    t = text.lower()
    t = re.sub(r"(?<=\d)[ .,](?=\d{3}(?!\d))", "", t)  # 1 500 000 / 1.500.000 -> 1500000
    mult = {"кк": 10**6, "млн": 10**6, "тыс": 10**3, "к": 10**3, "k": 10**3}
    out = set()
    for num, suf in re.findall(r"(\d+(?:[.,]\d+)?)\s*(кк|млн|тыс|к|k)?", t):
        out.add(int(round(float(num.replace(",", ".")) * mult.get(suf, 1))))
    return out


def match_car(name: str, price: int, text: str) -> bool:
    if price not in parse_prices(text):
        return False
    words = norm(text).split()
    for variant in name.split("|"):
        tokens = [t for t in norm(variant).split() if not t.isdigit()]
        if tokens and all(
            any(t == w or difflib.SequenceMatcher(None, t, w).ratio() >= 0.8 for w in words)
            for t in tokens
        ):
            return True
    return False


def parse_number(text: str) -> Optional[float]:
    if not re.fullmatch(r"\s*[-−]?\d+([.,]\d+)?\s*", text):
        return None
    return float(text.replace("−", "-").replace(",", "."))


def gen_calc() -> tuple[str, int]:
    a, b = random.randint(11, 99), random.randint(11, 99)
    c, d = random.randint(2, 12), random.randint(2, 9)
    expr = random.choice([
        f"{a} + {b} × {c}",
        f"{a} × {c} − {b}",
        f"{c * d} ÷ {d} + {a} × {c}",
        f"{a} × {c} + {b} × {d}",
        f"({a} + {b}) × {c}",
    ])
    ans = eval(expr.replace("×", "*").replace("÷", "/").replace("−", "-"))  # строка собрана ботом
    return expr, int(round(ans))


# ───────────────────────── Игровая логика ─────────────────────────
def scoreboard(g: Game) -> str:
    if not g.scores:
        return "—"
    rows = sorted(g.scores.items(), key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    return "\n".join(f"{medals[i] if i < 3 else '▫️'} <@{u}> — **{s}**" for i, (u, s) in enumerate(rows))


async def update_panel(g: Game):
    if not g.panel:
        return
    k = KINDS[g.kind]
    rounds = f"{g.round_no}/{g.max_rounds}" if g.max_rounds else str(g.round_no)
    e = discord.Embed(title=f"🎛 Панель ведущего — {k['title']}", color=0x5865F2)
    e.add_field(name="Раунд", value=f"{rounds} • {'🟢 идёт' if g.open else '⚪ ждёт запуска'}")
    e.add_field(name="Приз", value=g.prize)
    e.add_field(name="Счёт", value=scoreboard(g), inline=False)
    try:
        await g.panel.edit(embed=e)
    except discord.HTTPException:
        pass


async def start_round(g: Game, answer: str, price: Optional[int], image_url: Optional[str], text: str = ""):
    k = KINDS[g.kind]
    g.round_no += 1
    g.answer, g.price, g.open = answer, price, True
    rounds = f"{g.round_no}/{g.max_rounds}" if g.max_rounds else str(g.round_no)
    e = discord.Embed(title=f"{k['title']} — раунд {rounds}", description=text, color=0xF1C40F)
    if image_url:
        e.set_image(url=image_url)
    await g.channel.send(embed=e)
    await update_panel(g)


async def next_calc(g: Game):
    expr, ans = gen_calc()
    await start_round(g, str(ans), None, None, text=f"## {expr} = ?")


async def auto_next(g: Game, round_no: int):
    await asyncio.sleep(3)
    if not g.finished and not g.open and g.round_no == round_no and g.kind == "calc":
        await next_calc(g)


async def close_round(g: Game):
    """после закрытия раунда: либо финал, либо авто-следующий пример"""
    if g.max_rounds and g.round_no >= g.max_rounds:
        await finish(g)
    else:
        await update_panel(g)
        if g.kind == "calc":
            asyncio.create_task(auto_next(g, g.round_no))


async def win_round(g: Game, user_id: int):
    g.open = False
    g.scores[user_id] = g.scores.get(user_id, 0) + 1
    shown = f"\nПравильный ответ: **{g.answer}**" if g.answer else ""
    await g.channel.send(f"{KINDS[g.kind]['emoji']} <@{user_id}> получает балл!{shown}")
    await close_round(g)


async def finish(g: Game):
    if g.finished:
        return
    g.finished, g.open = True, False
    if g.timer and g.timer is not asyncio.current_task():
        g.timer.cancel()
    GAMES.pop(g.channel.id, None)
    if g.scores:
        top = max(g.scores.values())
        winners = [u for u, s in g.scores.items() if s == top]
        who = " и ".join(f"<@{u}>" for u in winners)
        head = f"🏆 Победитель: {who} ({top} б.)" if len(winners) == 1 else f"🤝 Ничья: {who} ({top} б.)"
        head += f"\nПриз: {g.prize}👑"
    else:
        head = "Победителя нет — никто не набрал баллов."
    e = discord.Embed(title=f"🏁 «{KINDS[g.kind]['title']}» завершено", description=f"{head}\n\n{scoreboard(g)}", color=0x57F287)
    await g.channel.send(embed=e)
    if g.panel:
        try:
            await g.panel.edit(view=None)
        except discord.HTTPException:
            pass


async def timeout_task(g: Game, minutes: int):
    await asyncio.sleep(minutes * 60)
    if not g.finished:
        await g.channel.send("⏰ Время вышло!")
        await finish(g)


async def handle_answer(g: Game, m: discord.Message):
    async with g.lock:
        if not g.open:
            return
        text = m.content
        if g.kind == "calc":
            val = parse_number(text)
            if val is None:
                return
            if abs(val - float(g.answer)) < 1e-9:
                await m.add_reaction("✅")
                await win_round(g, m.author.id)
            else:
                await m.add_reaction("❌")
        elif g.kind == "guess":
            if match_text(g.answer, text):
                await m.add_reaction("✅")
                await win_round(g, m.author.id)
            elif len(text) <= 60:
                await m.add_reaction("❌")
        elif g.kind == "turbo":
            if g.price is not None and match_car(g.answer, g.price, text):
                await m.add_reaction("💍")
                await win_round(g, m.author.id)
            elif len(text) <= 80:
                await m.add_reaction("❌")
        elif g.kind == "thing":
            # если ведущий указал ответ — победитель определяется автоматически
            if g.answer and match_text(g.answer, text):
                await m.add_reaction("💍")
                await win_round(g, m.author.id)


# ───────────────────────── Клиент ─────────────────────────
intents = discord.Intents.default()
intents.message_content = True


class MPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Бот запущен как {self.user}")

    async def on_message(self, m: discord.Message):
        if m.author.bot or not m.guild:
            return
        g = GAMES.get(m.channel.id)
        if g and g.open and m.author.id != g.host_id:
            try:
                await handle_answer(g, m)
            except discord.HTTPException:
                pass


client = MPBot()
tree = client.tree


# ───────────────────────── Панель ведущего ─────────────────────────
class RoundModal(discord.ui.Modal):
    def __init__(self, g: Game):
        super().__init__(title="Новый раунд")
        self.g = g
        self.answer = discord.ui.TextInput(
            label="Ответ (варианты через |)" if g.kind != "thing" else "Ответ (необязательно)",
            required=g.kind != "thing", max_length=200)
        self.add_item(self.answer)
        self.price = None
        if g.kind == "turbo":
            self.price = discord.ui.TextInput(label="Гос. цена (числом)", max_length=20)
            self.add_item(self.price)
        self.image = discord.ui.TextInput(label="Ссылка на картинку (необязательно)", required=False, max_length=400)
        self.add_item(self.image)

    async def on_submit(self, interaction: discord.Interaction):
        price = None
        if self.price is not None:
            found = parse_prices(self.price.value)
            if not found:
                return await interaction.response.send_message("Не понял цену 🤔", ephemeral=True)
            price = max(found)
        await launch_round(interaction, self.g, self.answer.value.strip(), price, self.image.value.strip() or None)


async def launch_round(interaction, g: Game, answer: str, price: Optional[int], image_url: Optional[str]):
    if g.finished:
        return await interaction.response.send_message("Игра уже завершена.", ephemeral=True)
    if g.open:
        return await interaction.response.send_message("Сначала закончи текущий раунд (⏭ Пропустить).", ephemeral=True)
    if g.max_rounds and g.round_no >= g.max_rounds:
        return await interaction.response.send_message("Все раунды уже сыграны — жми 🏁 Завершить.", ephemeral=True)
    if g.kind == "turbo" and price is None:
        return await interaction.response.send_message("Для Turbo нужна гос. цена.", ephemeral=True)
    await interaction.response.send_message(f"✅ Раунд запущен. Ответ: **{answer or '—'}**", ephemeral=True)
    await start_round(g, answer, price, image_url)


class PanelView(discord.ui.View):
    def __init__(self, g: Game):
        super().__init__(timeout=None)
        self.g = g
        self.next_btn.label = "▶ Следующий пример" if g.kind == "calc" else "🖼 Новый раунд"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_host(self.g, interaction.user):
            return True
        await interaction.response.send_message("Это панель ведущего 🙂", ephemeral=True)
        return False

    @discord.ui.button(label="Дальше", style=discord.ButtonStyle.success)
    async def next_btn(self, interaction: discord.Interaction, _):
        g = self.g
        if g.finished:
            return await interaction.response.send_message("Игра завершена.", ephemeral=True)
        if g.open:
            return await interaction.response.send_message("Сначала закончи текущий раунд (⏭ Пропустить).", ephemeral=True)
        if g.max_rounds and g.round_no >= g.max_rounds:
            return await interaction.response.send_message("Все раунды сыграны — жми 🏁 Завершить.", ephemeral=True)
        if g.kind == "calc":
            await interaction.response.defer()
            await next_calc(g)
        else:
            await interaction.response.send_modal(RoundModal(g))

    @discord.ui.button(label="⏭ Пропустить", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, _):
        g = self.g
        if not g.open:
            return await interaction.response.send_message("Сейчас нет активного раунда.", ephemeral=True)
        await interaction.response.defer()
        async with g.lock:
            g.open = False
            await g.channel.send(f"⏭ Раунд пропущен." + (f" Ответ: **{g.answer}**" if g.answer else ""))
            await close_round(g)

    @discord.ui.button(label="🏁 Завершить", style=discord.ButtonStyle.danger)
    async def finish_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        await finish(self.g)


# ───────────────────────── Команды ─────────────────────────
@tree.command(name="стартмп", description="Запустить мероприятие в этом канале")
@app_commands.guild_only()
@app_commands.describe(game="Какое мероприятие", prize="Призовой фонд")
@app_commands.choices(game=[
    app_commands.Choice(name="Калькулятор", value="calc"),
    app_commands.Choice(name="Угадайка", value="guess"),
    app_commands.Choice(name="Turbo", value="turbo"),
    app_commands.Choice(name="Угадай Вещь", value="thing"),
])
async def cmd_start(interaction: discord.Interaction, game: app_commands.Choice[str], prize: str):
    if not can_start(interaction.user):
        return await interaction.response.send_message("Нет прав вести мероприятия.", ephemeral=True)
    if interaction.channel_id in GAMES:
        return await interaction.response.send_message("В этом канале уже идёт игра. Заверши её: /стоп", ephemeral=True)

    k = KINDS[game.value]
    g = Game(kind=game.value, host_id=interaction.user.id, channel=interaction.channel,
             prize=prize, max_rounds=k["rounds"])
    GAMES[interaction.channel_id] = g
    await interaction.response.send_message("🚀 Мероприятие запущено!", ephemeral=True)

    await interaction.channel.send(announcement(game.value, prize),
                                   allowed_mentions=discord.AllowedMentions(roles=True))
    g.panel_view = PanelView(g)
    g.panel = await interaction.channel.send(embed=discord.Embed(title="🎛 Панель ведущего"), view=g.panel_view)
    await update_panel(g)
    if k["minutes"]:
        g.timer = asyncio.create_task(timeout_task(g, k["minutes"]))


@tree.command(name="раунд", description="Новый раунд с картинкой (Угадайка / Turbo / Угадай Вещь)")
@app_commands.guild_only()
@app_commands.describe(image="Картинка (флаг, авто, замазанное фото)",
                       answer="Ответ (варианты через |). Для «Угадай Вещь» можно пропустить",
                       price="Гос. цена — только для Turbo")
async def cmd_round(interaction: discord.Interaction, image: discord.Attachment,
                    answer: Optional[str] = None, price: Optional[int] = None):
    g = GAMES.get(interaction.channel_id)
    if not g or not is_host(g, interaction.user):
        return await interaction.response.send_message("Тут нет твоей активной игры.", ephemeral=True)
    if g.kind == "calc":
        return await interaction.response.send_message("В Калькуляторе примеры бот генерирует сам 😉", ephemeral=True)
    if g.kind != "thing" and not answer:
        return await interaction.response.send_message("Укажи ответ.", ephemeral=True)
    await launch_round(interaction, g, (answer or "").strip(), price, image.url)


@tree.command(name="счет", description="Показать текущий счёт")
@app_commands.guild_only()
async def cmd_score(interaction: discord.Interaction):
    g = GAMES.get(interaction.channel_id)
    if not g:
        return await interaction.response.send_message("Тут нет активной игры.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(title="Счёт", description=scoreboard(g)))


@tree.command(name="стоп", description="Завершить мероприятие и объявить победителя")
@app_commands.guild_only()
async def cmd_stop(interaction: discord.Interaction):
    g = GAMES.get(interaction.channel_id)
    if not g or not is_host(g, interaction.user):
        return await interaction.response.send_message("Тут нет твоей активной игры.", ephemeral=True)
    await interaction.response.send_message("Завершаю…", ephemeral=True)
    await finish(g)


# ───── «Угадай Вещь»: зажми сообщение игрока → Приложения → реакция ─────
async def mark(interaction: discord.Interaction, message: discord.Message, emoji: str, win: bool = False):
    g = GAMES.get(interaction.channel_id)
    if not g or not is_host(g, interaction.user):
        return await interaction.response.send_message("Нет активной игры или ты не ведущий.", ephemeral=True)
    if win and (not g.open or message.author.bot or message.author.id == g.host_id):
        return await interaction.response.send_message("Сейчас нельзя назначить победителя.", ephemeral=True)
    await interaction.response.send_message("Готово ✔", ephemeral=True)
    await message.add_reaction(emoji)
    if win:
        async with g.lock:
            if g.open:
                await win_round(g, message.author.id)


@tree.context_menu(name="✅ Близко")
async def ctx_close(interaction: discord.Interaction, message: discord.Message):
    await mark(interaction, message, "✅")


@tree.context_menu(name="❌ Далеко")
async def ctx_far(interaction: discord.Interaction, message: discord.Message):
    await mark(interaction, message, "❌")


@tree.context_menu(name="💍 Победитель")
async def ctx_win(interaction: discord.Interaction, message: discord.Message):
    await mark(interaction, message, "💍", win=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Не задана переменная окружения DISCORD_TOKEN — добавь токен бота в настройках хостинга.")
    client.run(TOKEN)
