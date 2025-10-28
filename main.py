import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Deque, Optional, List
from collections import deque

import random

import discord
from discord import app_commands
from discord.ext import commands


from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument
from telethon.errors import UsernameNotOccupiedError
from dotenv import load_dotenv

from yt_dlp import YoutubeDL
import re

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")  # например: @my_music_channel
TEMP_DIR = os.getenv("TEMP_DIR", ".cache")

if not all([DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL]):
    raise RuntimeError("Заполните DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL в .env")

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "extract_flat": False,
    "default_search": "ytsearch",
}
YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.I)

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
    filepath: Optional[str] = None           # локальный файл (TG)
    source_msg_id: Optional[int] = None
    stream_url: Optional[str] = None         # прямой поток (YouTube)

@dataclass
class GuildPlayer:
    voice: Optional[discord.VoiceClient] = None
    queue: Deque[Track] = field(default_factory=deque)
    now_playing: Optional[Track] = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop_current: bool = False  # <— НОВОЕ: зацикливать текущий трек
    voice: Optional[discord.VoiceClient] = None
    queue: Deque[Track] = field(default_factory=deque)
    now_playing: Optional[Track] = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

players: dict[int, GuildPlayer] = {}

SUPPORTED_AUDIO_MIME_PREFIXES = ("audio/",)
SUPPORTED_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}

def channel_has_listeners(vc: Optional[discord.VoiceClient]) -> bool:
    """Есть ли в голосовом канале хоть один не-бот кроме нашего бота."""
    if not vc or not vc.channel:
        return False
    for m in vc.channel.members:
        if not m.bot:
            return True
    return False

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

async def ytdlp_resolve(query: str) -> tuple[str, str]:
    """
    Возвращает (title, direct_audio_url) для YouTube.
    Поддерживает: прямая ссылка на видео, плейлист (берёт первый), текстовый поиск.
    """
    with YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        title = info.get("title") or "YouTube Audio"
        direct_url = info.get("url")

        if not direct_url:
            # запасной путь: из форматов берём любой аудио-поток
            for f in reversed(info.get("formats") or []):
                if f.get("acodec") and f.get("url"):
                    direct_url = f["url"]
                    break

        if not direct_url:
            raise RuntimeError("Не удалось получить прямой аудио-URL (YouTube)")

        return title, direct_url

async def play_next(guild: discord.Guild):
    player = await ensure_player(guild)
    vc = player.voice

    if not vc or not vc.is_connected():
        return

    if not channel_has_listeners(vc):
        player.loop_current = False
        player.now_playing = None
        return

    if vc.is_playing() or vc.is_paused():
        return

    if not player.queue and player.loop_current and player.now_playing:
        t = player.now_playing
        looped = Track(title=t.title, filepath=t.filepath, source_msg_id=t.source_msg_id, stream_url=t.stream_url)
        player.queue.appendleft(looped)

    if not player.queue:
        player.now_playing = None
        return

    track = player.queue.popleft()
    player.now_playing = track
    # если это TG-трек, у которого ещё нет локального файла — докачаем прямо сейчас
    if getattr(track, "filepath", None) is None and getattr(track, "source_msg_id", None) is not None:
        try:
            entity = await get_tg_entity()
            msg = await tele_client.get_messages(entity, ids=track.source_msg_id)
            path = await download_audio(msg, track.title)
            track.filepath = path
        except Exception as e:
            print(f"Не удалось скачать TG-трек {track.title}: {e}")
            # пробуем перейти к следующему
            fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
            return

    async def collect_all_tg_audios(max_items: int = 2000) -> list[tuple]:
        """
        Собирает до max_items аудиосообщений/документов из TELEGRAM_CHANNEL.
        Возвращает список (msg, title).
        """
        entity = await get_tg_entity()
        results = []
        # идём от новых к старым; увеличь limit, если хочешь быстрее (но не все клиенты переварят 10k+)
        async for msg in tele_client.iter_messages(entity, limit=max_items):
            if isinstance(msg.media, MessageMediaDocument) and msg.file:
                mime = getattr(msg.file, "mime_type", "") or ""
                name = msg.file.name or f"audio_{msg.id}"
                ext = os.path.splitext(name)[1].lower()
                if mime.startswith(SUPPORTED_AUDIO_MIME_PREFIXES) or ext in SUPPORTED_EXTS:
                    title = (msg.message or name).strip()[:200] if msg.message else name
                    results.append((msg, title))
        return results

    def after_play(err):
        if err:
            print(f"FFmpeg error: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("play_next error:", e)

    reconnect_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

    if track.filepath:
        source = discord.FFmpegPCMAudio(track.filepath, before_options='-nostdin', options='-vn')
    elif track.stream_url:
        source = discord.FFmpegPCMAudio(track.stream_url, before_options=f"-nostdin {reconnect_opts}", options='-vn')
    else:
        print("track без источника, пропуск")
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        return

    vc.play(source, after=after_play)

    def after_play(err):
        if err:
            print(f"FFmpeg error: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("play_next error:", e)

    reconnect_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

    if getattr(track, "filepath", None):
        source = discord.FFmpegPCMAudio(track.filepath, before_options='-nostdin', options='-vn')
    elif getattr(track, "stream_url", None):
        source = discord.FFmpegPCMAudio(track.stream_url, before_options=f"-nostdin {reconnect_opts}", options='-vn')
    else:
        print("track без источника, пропуск")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        return

    vc.play(source, after=after_play)

async def _cmd_shuffle_all(interaction: discord.Interaction, limit: Optional[int] = None):
    """
    Собирает ВСЕ (или до limit) аудио из TG, перемешивает, ставит в очередь.
    Скачивание аудио будет непосредственно перед проигрыванием.
    """
    await interaction.response.defer(thinking=True)
    await connect_to_author_channel(interaction)
    player = await ensure_player(interaction.guild)

    # сколько максимума собирать
    max_items = limit or 100  # можно менять по вкусу
    all_tracks = await collect_all_tg_audios(max_items=max_items)
    if not all_tracks:
        await interaction.followup.send("В канале не найдено ни одного аудио.")
        return

    random.shuffle(all_tracks)

    # кладём в очередь «лёгкие» объекты (без предзагрузки файла)
    added = 0
    async with player.play_lock:
        for msg, title in all_tracks:
            player.queue.append(Track(title=title, source_msg_id=msg.id))  # filepath=None, докачаем в play_next
            added += 1

    await interaction.followup.send(f"Перемешал и добавил в очередь {added} трек(ов). Поехали! 🔀")
    await play_next(interaction.guild)

# --- СЛЭШ-КОМАНДЫ ---
# ==== COMMAND HELPERS =========================================================
async def _cmd_join(interaction: discord.Interaction):
    vc = await connect_to_author_channel(interaction)
    await interaction.response.send_message(f"Подключился к: **{vc.channel.name}**")

async def _cmd_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
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

async def _cmd_latest(interaction: discord.Interaction, n: Optional[int] = 10):
    await interaction.response.defer(thinking=True)
    results = await search_telegram_audios(None, limit=n or 10)
    if not results:
        await interaction.followup.send("В канале не найдено аудио")
        return
    lines = []
    for msg, title in results:
        ext = os.path.splitext(msg.file.name or "")[1] if msg.file else ""
        size_mb = (msg.file.size or 0) / (1024*1024) if msg.file else 0
        lines.append(f"• **{title}** {ext} — {size_mb:.1f} MB (id: `{msg.id}`)")
    await interaction.followup.send("Последние аудио:\n" + "\n".join(lines))

async def _cmd_queue(interaction: discord.Interaction):
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

async def _cmd_skip(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.stop()
        await interaction.response.send_message("⏭️ Пропустил")
    else:
        await interaction.response.send_message("Сейчас ничего не играет")

async def _cmd_pause(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        await interaction.response.send_message("⏸️ Пауза")
    else:
        await interaction.response.send_message("Нечего ставить на паузу")

async def _cmd_resume(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        await interaction.response.send_message("▶️ Продолжаю")
    else:
        await interaction.response.send_message("Нечего продолжать")

async def _cmd_stop(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    player.queue.clear()
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
    await interaction.response.send_message("⏹️ Остановил и очистил очередь")

async def _cmd_loop(interaction: discord.Interaction, enabled: bool = True):
    player = await ensure_player(interaction.guild)
    if not player.voice or not player.voice.is_connected():
        await interaction.response.send_message("Бот не в голосовом канале. Сначала /join.", ephemeral=True)
        return
    if not player.now_playing and not player.queue:
        await interaction.response.send_message("Нечего зацикливать — очередь пуста.", ephemeral=True)
        return
    player.loop_current = bool(enabled)
    await interaction.response.send_message("🔁 Повтор включён" if enabled else "⏹️ Повтор выключен")
    await play_next(interaction.guild)

async def _cmd_loopstatus(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    state = "включён 🔁" if player.loop_current else "выключен ⏹️"
    np = f" | сейчас: **{player.now_playing.title}**" if player.now_playing else ""
    await interaction.response.send_message(f"Повтор {state}{np}")
# =============================================================================

@tree.command(name="yt", description="Воспроизвести аудиодорожку с YouTube (поиск или ссылка)")
@app_commands.describe(query="Запрос поиска или URL YouTube")
async def yt(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    try:
        await connect_to_author_channel(interaction)
        player = await ensure_player(interaction.guild)

        title, direct_url = await ytdlp_resolve(query)
        async with player.play_lock:
            track = Track(title=title, stream_url=direct_url)
            player.queue.append(track)
            await interaction.followup.send(f"Добавлено с YouTube: **{track.title}**")
            await play_next(interaction.guild)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка YouTube: {e}")

@tree.command(name="ютуб", description="Воспроизвести аудиодорожку с YouTube (поиск или ссылка)")
@app_commands.describe(запрос="Запрос поиска или URL YouTube")
async def ютуб(interaction: discord.Interaction, запрос: str):
    await yt(interaction, запрос)  # просто прокси на yt

@tree.command(name="join", description="Join your voice channel")
async def join(interaction: discord.Interaction):
    try:
        await _cmd_join(interaction)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)

@tree.command(name="shuffleall", description="Shuffle and queue all audios from the Telegram channel")
@app_commands.describe(limit="Max items to scan (default 100)")
async def shuffleall(interaction: discord.Interaction, limit: Optional[int] = None):
    try:
        await _cmd_shuffle_all(interaction, limit)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(name="перемешать", description="Перемешать и поставить в очередь все аудио из TG-канала")
@app_commands.describe(limit="Максимум для сканирования (по умолчанию 2000)")
async def перемешать(interaction: discord.Interaction, limit: Optional[int] = None):
    try:
        await _cmd_shuffle_all(interaction, limit)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(name="зайти", description="Зайти в ваш голосовой канал")
async def зайти(interaction: discord.Interaction):
    try:
        await _cmd_join(interaction)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)

# PLAY
@tree.command(name="play", description="Play from Telegram channel by query")
@app_commands.describe(query="Search text")
async def play(interaction: discord.Interaction, query: str):
    try:
        await _cmd_play(interaction, query)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(name="воспроизвести", description="Воспроизвести трек из Telegram-канала по запросу")
@app_commands.describe(запрос="Название/фраза для поиска")
async def воспроизвести(interaction: discord.Interaction, запрос: str):
    try:
        await _cmd_play(interaction, запрос)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

# LATEST
@tree.command(name="latest", description="Show last N audios from Telegram")
@app_commands.describe(n="How many (default 10)")
async def latest(interaction: discord.Interaction, n: Optional[int] = 10):
    try:
        await _cmd_latest(interaction, n)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(name="последние", description="Показать последние N треков из Telegram-канала")
@app_commands.describe(n="Сколько показать (по умолчанию 10)")
async def последние(interaction: discord.Interaction, n: Optional[int] = 10):
    try:
        await _cmd_latest(interaction, n)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

# QUEUE
@tree.command(name="queue", description="Show queue")
async def queue(interaction: discord.Interaction):
    await _cmd_queue(interaction)

@tree.command(name="очередь", description="Показать очередь")
async def очередь(interaction: discord.Interaction):
    await _cmd_queue(interaction)

# SKIP
@tree.command(name="skip", description="Skip current track")
async def skip(interaction: discord.Interaction):
    await _cmd_skip(interaction)

@tree.command(name="пропустить", description="Пропустить текущий трек")
async def пропустить(interaction: discord.Interaction):
    await _cmd_skip(interaction)

# PAUSE / RESUME
@tree.command(name="pause", description="Pause")
async def pause(interaction: discord.Interaction):
    await _cmd_pause(interaction)

@tree.command(name="пауза", description="Пауза")
async def пауза(interaction: discord.Interaction):
    await _cmd_pause(interaction)

@tree.command(name="resume", description="Resume")
async def resume(interaction: discord.Interaction):
    await _cmd_resume(interaction)

@tree.command(name="продолжить", description="Продолжить")
async def продолжить(interaction: discord.Interaction):
    await _cmd_resume(interaction)

# STOP
@tree.command(name="stop", description="Stop and clear queue")
async def stop(interaction: discord.Interaction):
    await _cmd_stop(interaction)

@tree.command(name="остановить", description="Остановить и очистить очередь")
async def остановить(interaction: discord.Interaction):
    await _cmd_stop(interaction)

# LOOP
@tree.command(name="loop", description="Loop current track while listeners exist")
@app_commands.describe(enabled="Enable or disable")
async def loop(interaction: discord.Interaction, enabled: Optional[bool] = True):
    await _cmd_loop(interaction, enabled)

@tree.command(name="повтор", description="Зациклить текущий трек (пока есть слушатели)")
@app_commands.describe(вкл="True — включить, False — выключить")
async def повтор(interaction: discord.Interaction, вкл: Optional[bool] = True):
    await _cmd_loop(interaction, bool(вкл))

# LOOP STATUS
@tree.command(name="loopstatus", description="Show loop status")
async def loopstatus(interaction: discord.Interaction):
    await _cmd_loopstatus(interaction)

@tree.command(name="повторстатус", description="Показать состояние повтора")
async def повторстатус(interaction: discord.Interaction):
    await _cmd_loopstatus(interaction)

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