import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Deque, Optional, List
from collections import deque


import discord
from discord import app_commands
from discord.ext import commands


from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument
from telethon.errors import UsernameNotOccupiedError
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")  # например: @my_music_channel
TEMP_DIR = os.getenv("TEMP_DIR", ".cache")

if not all([DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL]):
    raise RuntimeError("Заполните DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL в .env")

os.makedirs(TEMP_DIR, exist_ok=True)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- Telegram client (один на процесс) ---
tele_client = TelegramClient("tg_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)

# --- Очередь треков на сервер (гильдию) ---
@dataclass
class Track:
    title: str
    filepath: str
    source_msg_id: int

@dataclass
class GuildPlayer:
    voice: Optional[discord.VoiceClient] = None
    queue: Deque[Track] = field(default_factory=deque)
    now_playing: Optional[Track] = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

players: dict[int, GuildPlayer] = {}

SUPPORTED_AUDIO_MIME_PREFIXES = ("audio/",)
SUPPORTED_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}



async def ensure_player(guild: discord.Guild) -> GuildPlayer:
    if guild.id not in players:
        players[guild.id] = GuildPlayer()
    return players[guild.id]

async def get_tg_entity():
    chan = (TELEGRAM_CHANNEL or "").strip()
    if not chan:
        raise RuntimeError("TELEGRAM_CHANNEL пуст")
    try:
        # принимает @username И полные t.me-ссылки (включая invite)
        return await tele_client.get_entity(chan)
    except Exception as e:
        raise RuntimeError(f"Не удалось получить канал по TELEGRAM_CHANNEL='{chan}': {e}")

# и в search_telegram_audios замените:
# entity = await tele_client.get_entity(TELEGRAM_CHANNEL)


async def connect_to_author_channel(interaction: discord.Interaction) -> discord.VoiceClient:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise RuntimeError("Команда должна вызываться участником сервера")
    voice_state = interaction.user.voice
    if not voice_state or not voice_state.channel:
        raise RuntimeError("Зайдите в голосовой канал перед вызовом команды")
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_connected():
        if player.voice.channel.id != voice_state.channel.id:
            await player.voice.move_to(voice_state.channel)
    else:
        player.voice = await voice_state.channel.connect(self_deaf=True)
    return player.voice

async def search_telegram_audios(query: Optional[str], limit: int = 20):
    # Ищем сообщения с документами-аудио
    try:
        entity = await get_tg_entity()   # <-- ВЫЗОВ ТУТ, внутри async-функции
    except Exception as e:
        raise RuntimeError(str(e))

    results = []
    async for msg in tele_client.iter_messages(entity, search=query or None, limit=200):
        if isinstance(msg.media, MessageMediaDocument) and msg.file:
            mime = getattr(msg.file, "mime_type", "") or ""
            name = msg.file.name or f"audio_{msg.id}"
            ext = os.path.splitext(name)[1].lower()
            if mime.startswith(SUPPORTED_AUDIO_MIME_PREFIXES) or ext in SUPPORTED_EXTS:
                title = (msg.message or name).strip()[:200] if msg.message else name
                results.append((msg, title))
                if len(results) >= limit:
                    break
    return results

async def download_audio(msg, title: str) -> str:
    # Скачиваем в TEMP_DIR с безопасным именем
    safe = "".join(c for c in title if c.isalnum() or c in " _-()[]{}.,!")
    base = safe or f"audio_{msg.id}"
    tmp_path = os.path.join(TEMP_DIR, f"{base}_{msg.id}")
    path = await tele_client.download_media(msg, file=tmp_path)
    return path

async def play_next(guild: discord.Guild):
    player = await ensure_player(guild)
    if not player.voice or not player.voice.is_connected():
        return
    if player.voice.is_playing() or player.voice.is_paused():
        return
    if not player.queue:
        player.now_playing = None
        return

    track = player.queue.popleft()
    player.now_playing = track

    def after_play(err):
        if err:
            print(f"FFmpeg error: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("play_next error:", e)

    source = discord.FFmpegPCMAudio(track.filepath, before_options="-nostdin", options="-vn")
    player.voice.play(source, after=after_play)

# --- СЛЭШ-КОМАНДЫ ---

@tree.command(description="Зайти в ваш голосовой канал")
async def join(interaction: discord.Interaction):
    try:
        vc = await connect_to_author_channel(interaction)
        await interaction.response.send_message(f"Подключился к: **{vc.channel.name}**")
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)

@tree.command(description="Воспроизвести трек из Telegram-канала по запросу")
@app_commands.describe(query="Название/фраза для поиска в канале Telegram")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    try:
        await connect_to_author_channel(interaction)
        player = await ensure_player(interaction.guild)
        async with player.play_lock:
            found = await search_telegram_audios(query, limit=1)
            if not found:
                await interaction.followup.send("Не нашёл подходящих аудио в канале Telegram 🤷‍♂️")
                return
            msg, title = found[0]
            path = await download_audio(msg, title)
            track = Track(title=title, filepath=path, source_msg_id=msg.id)
            player.queue.append(track)
            await interaction.followup.send(f"Добавлено в очередь: **{track.title}**")
            await play_next(interaction.guild)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(description="Показать последние N треков из Telegram-канала")
@app_commands.describe(n="Сколько показать (по умолчанию 10)")
async def latest(interaction: discord.Interaction, n: Optional[int] = 10):
    await interaction.response.defer(thinking=True)
    try:
        results = await search_telegram_audios(None, limit=n or 10)
        if not results:
            await interaction.followup.send("В канале не найдено аудио")
            return
        lines = []
        for msg, title in results:
            ext = os.path.splitext(msg.file.name or "")[1] if msg.file else ""
            size_mb = (msg.file.size or 0) / (1024 * 1024) if msg.file else 0
            lines.append(f"• **{title}** {ext} — {size_mb:.1f} MB (id: `{msg.id}`)")
        await interaction.followup.send("Последние аудио:\n" + "\n".join(lines))
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(description="Показать очередь воспроизведения")
async def queue(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    parts = []
    if player.now_playing:
        parts.append(f"▶️ Сейчас: **{player.now_playing.title}**")
    if player.queue:
        for i, t in enumerate(list(player.queue)[:20], 1):
            parts.append(f"{i}. {t.title}")
    if not parts:
        parts.append("Очередь пуста")
    await interaction.response.send_message("\n".join(parts))

@tree.command(description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.stop()
        await interaction.response.send_message("⏭️ Пропустил")
    else:
        await interaction.response.send_message("Сейчас ничего не играет")

@tree.command(description="Пауза")
async def pause(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        await interaction.response.send_message("⏸️ Пауза")
    else:
        await interaction.response.send_message("Нечего ставить на паузу")

@tree.command(description="Продолжить")
async def resume(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        await interaction.response.send_message("▶️ Продолжаю")
    else:
        await interaction.response.send_message("Нечего продолжать")

@tree.command(description="Остановить и очистить очередь")
async def stop(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    player.queue.clear()
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
    await interaction.response.send_message("⏹️ Остановил и очистил очередь")

async def debug_telegram_startup_check():
    print("=== TELETHON DEBUG START ===")
    print("TELEGRAM_CHANNEL =", repr(TELEGRAM_CHANNEL))

    # 1) Проверяем — подключён ли Telethon
    if not tele_client.is_connected():
        print("Telethon: not connected — connecting...")
        await tele_client.connect()

    # 2) Проверяем авторизацию (нужно login через номер)
    if not await tele_client.is_user_authorized():
        print("❌ Telethon НЕ авторизован — нужно перезапустить и войти через номер!")
        print("=== TELETHON DEBUG END ===")
        return
    else:
        me = await tele_client.get_me()
        print(f"✅ Telethon авторизован как: {me.first_name} (@{me.username}) id={me.id}")

    # 3) Пробуем получить канал/группу
    try:
        entity = await get_tg_entity()
        print(f"✅ Канал/группа доступна: id={entity.id}, title='{getattr(entity,'title',None)}'")
    except Exception as e:
        print("❌ Ошибка получения канала:", e)

    print("=== TELETHON DEBUG END ===")

@bot.event
async def on_ready():
    # запускаем Telegram клиент, если ещё не запущен
    if not tele_client.is_connected():
        # start() запустит интерактивный логин, если сессии нет
        await tele_client.start()
    try:
        await tree.sync()
        print(f"Синхронизированы слэш-команды для {bot.user}")
    except Exception as e:
        print("Не удалось синхронизировать команды:", e)
    print(f"Готово: {bot.user} (ID: {bot.user.id})")

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    # Инициализируем Telegram клиент заранее, чтобы создать сессию
    async def run():
        await tele_client.start()
        await main()
    try:
        asyncio.run(run())
    finally:
        if tele_client.is_connected():
            asyncio.run(tele_client.disconnect())