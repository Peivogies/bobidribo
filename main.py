import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from typing import Deque, Optional
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

# ────────────────────────────────────────────────────────────────────────────
# Конфиг
# ────────────────────────────────────────────────────────────────────────────

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")
TEMP_DIR = os.getenv("TEMP_DIR", ".cache")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "tg_session_server")

if not all([DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL]):
    raise RuntimeError("Заполните DISCORD_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL в .env")

os.makedirs(TEMP_DIR, exist_ok=True)

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "extract_flat": False,
    "default_search": "ytsearch",
    "geo_bypass": True,
}
YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.I)
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES")
if YTDLP_COOKIES and os.path.exists(YTDLP_COOKIES):
    YDL_OPTS["cookiefile"] = YTDLP_COOKIES

# ────────────────────────────────────────────────────────────────────────────
# Discord bot
# ────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ────────────────────────────────────────────────────────────────────────────
# Telethon client
# ────────────────────────────────────────────────────────────────────────────

tele_client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

# ────────────────────────────────────────────────────────────────────────────
# Модель данных
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    title: str
    filepath: Optional[str] = None        # локальный файл (TG)
    source_msg_id: Optional[int] = None   # id сообщения TG
    stream_url: Optional[str] = None      # прямой поток (YouTube)

@dataclass
class GuildPlayer:
    voice: Optional[discord.VoiceClient] = None
    queue: Deque[Track] = field(default_factory=deque)
    now_playing: Optional[Track] = None
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop_current: bool = False            # зацикливать текущий трек

players: dict[int, GuildPlayer] = {}

SUPPORTED_AUDIO_MIME_PREFIXES = ("audio/",)
SUPPORTED_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}

# ────────────────────────────────────────────────────────────────────────────
# Хелперы
# ────────────────────────────────────────────────────────────────────────────

def channel_has_listeners(vc: Optional[discord.VoiceClient]) -> bool:
    if not vc or not vc.channel:
        return False
    return any(not m.bot for m in vc.channel.members)

async def ensure_player(guild: discord.Guild) -> GuildPlayer:
    if guild.id not in players:
        players[guild.id] = GuildPlayer()
    return players[guild.id]

async def get_tg_entity():
    chan = (TELEGRAM_CHANNEL or "").strip()
    if not chan:
        raise RuntimeError("TELEGRAM_CHANNEL пуст")
    try:
        return await tele_client.get_entity(chan)
    except Exception as e:
        raise RuntimeError(f"Не удалось получить канал по TELEGRAM_CHANNEL='{chan}': {e}")

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
    entity = await get_tg_entity()
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
    safe = "".join(c for c in title if c.isalnum() or c in " _-()[]{}.,!")
    base = safe or f"audio_{msg.id}"
    tmp_path = os.path.join(TEMP_DIR, f"{base}_{msg.id}")
    return await tele_client.download_media(msg, file=tmp_path)

async def collect_all_tg_audios(max_items: int = 2000) -> list[tuple]:
    """
    Собирает до max_items аудио-документов из TELEGRAM_CHANNEL.
    Возвращает список пар (msg, title).
    """
    entity = await get_tg_entity()
    results = []
    async for msg in tele_client.iter_messages(entity, limit=max_items):
        if isinstance(msg.media, MessageMediaDocument) and msg.file:
            mime = getattr(msg.file, "mime_type", "") or ""
            name = msg.file.name or f"audio_{msg.id}"
            ext = os.path.splitext(name)[1].lower()
            if mime.startswith(SUPPORTED_AUDIO_MIME_PREFIXES) or ext in SUPPORTED_EXTS:
                title = (msg.message or name).strip()[:200] if msg.message else name
                results.append((msg, title))
    return results

async def ytdlp_resolve(query: str) -> tuple[str, str]:
    with YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        title = info.get("title") or "YouTube Audio"
        direct_url = info.get("url")
        if not direct_url:
            for f in reversed(info.get("formats") or []):
                if f.get("acodec") and f.get("url"):
                    direct_url = f["url"]
                    break
        if not direct_url:
            raise RuntimeError("Не удалось получить прямой аудио-URL (YouTube)")
        return title, direct_url

# ────────────────────────────────────────────────────────────────────────────
# Воспроизведение
# ────────────────────────────────────────────────────────────────────────────

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
        player.queue.appendleft(
            Track(title=t.title, filepath=t.filepath,
                  source_msg_id=t.source_msg_id, stream_url=t.stream_url)
        )

    if not player.queue:
        player.now_playing = None
        return

    track = player.queue.popleft()
    player.now_playing = track

    if track.filepath is None and track.source_msg_id is not None:
        try:
            entity = await get_tg_entity()
            msg = await tele_client.get_messages(entity, ids=track.source_msg_id)
            path = await download_audio(msg, track.title)
            track.filepath = path
        except Exception as e:
            print(f"[play_next] не удалось скачать TG-трек '{track.title}': {e}")
            fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
            try:
                fut.result()
            except Exception as ee:
                print("[play_next] ошибка при планировании следующего:", ee)
            return

    def after_play(err: Optional[Exception]):
        if err:
            print(f"[FFmpeg error]: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as ee:
            print("[play_next] ошибка при планировании следующего:", ee)

    reconnect_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

    if track.filepath:
        source = discord.FFmpegPCMAudio(
            track.filepath,
            before_options='-nostdin',
            options='-vn'
        )
    elif track.stream_url:
        source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=f"-nostdin {reconnect_opts}",
            options='-vn'
        )
    else:
        print(f"[play_next] у трека '{track.title}' нет источника (filepath/stream_url)")
        fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            fut.result()
        except Exception as ee:
            print("[play_next] ошибка при планировании следующего:", ee)
        return

    vc.play(source, after=after_play)

# ────────────────────────────────────────────────────────────────────────────
# СЛЭШ-КОМАНДЫ
# ────────────────────────────────────────────────────────────────────────────

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

async def _cmd_loop(interaction: discord.Interaction):
    player = await ensure_player(interaction.guild)
    player.loop_current = not player.loop_current
    await interaction.response.send_message(
        f"🔁 Зацикливание текущего трека: **{'включено' if player.loop_current else 'выключено'}**"
    )

async def _cmd_yt(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    await connect_to_author_channel(interaction)
    player = await ensure_player(interaction.guild)
    try:
        title, direct_url = await asyncio.get_event_loop().run_in_executor(None, ytdlp_resolve, query)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка YouTube: {e}")
        return
    async with player.play_lock:
        player.queue.append(Track(title=title, stream_url=direct_url))
    await interaction.followup.send(f"Добавлено из YouTube: **{title}**")
    await play_next(interaction.guild)

async def _cmd_shuffle_all(interaction: discord.Interaction, limit: Optional[int] = None):
    """
    Собирает ВСЕ (или до limit) аудио из TG, перемешивает, ставит в очередь.
    Скачивание — непосредственно перед проигрыванием.
    """
    await interaction.response.defer(thinking=True)
    await connect_to_author_channel(interaction)
    player = await ensure_player(interaction.guild)

    max_items = limit or 100
    all_tracks = await collect_all_tg_audios(max_items=max_items)
    if not all_tracks:
        await interaction.followup.send("В канале не найдено ни одного аудио.")
        return

    random.shuffle(all_tracks)

    added = 0
    async with player.play_lock:
        for msg, title in all_tracks:
            player.queue.append(Track(title=title, source_msg_id=msg.id))
            added += 1

    await interaction.followup.send(f"Перемешал и добавил в очередь {added} трек(ов). Поехали! 🔀")
    await play_next(interaction.guild)

# ────────────────────────────────────────────────────────────────────────────
# Регистрация слэш-команд
# ────────────────────────────────────────────────────────────────────────────

@tree.command(name="join", description="Зайти в ваш голосовой канал")
async def join_cmd(interaction: discord.Interaction): await _cmd_join(interaction)

@tree.command(name="play", description="Воспроизвести трек из Telegram по запросу")
@app_commands.describe(query="Название/фраза для поиска")
async def play_cmd(interaction: discord.Interaction, query: str): await _cmd_play(interaction, query)

@tree.command(name="latest", description="Показать последние N треков из Telegram-канала")
@app_commands.describe(n="Сколько показать (по умолчанию 10)")
async def latest_cmd(interaction: discord.Interaction, n: Optional[int] = 10): await _cmd_latest(interaction, n)

@tree.command(name="queue", description="Показать очередь воспроизведения")
async def queue_cmd(interaction: discord.Interaction): await _cmd_queue(interaction)

@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction): await _cmd_skip(interaction)

@tree.command(name="pause", description="Пауза")
async def pause_cmd(interaction: discord.Interaction): await _cmd_pause(interaction)

@tree.command(name="resume", description="Продолжить")
async def resume_cmd(interaction: discord.Interaction): await _cmd_resume(interaction)

@tree.command(name="stop", description="Остановить и очистить очередь")
async def stop_cmd(interaction: discord.Interaction): await _cmd_stop(interaction)

@tree.command(name="loop", description="Вкл/выкл зацикливание текущего трека")
async def loop_cmd(interaction: discord.Interaction): await _cmd_loop(interaction)

@tree.command(name="yt", description="Воспроизвести звук с YouTube (по ссылке или поиску)")
@app_commands.describe(query="Ссылка на YouTube или запрос для поиска")
async def yt_cmd(interaction: discord.Interaction, query: str): await _cmd_yt(interaction, query)

@tree.command(name="shuffleall", description="Перемешать и добавить все треки из TG-канала")
@app_commands.describe(limit="Сколько максимум собрать (по умолчанию 100)")
async def shuffleall_cmd(interaction: discord.Interaction, limit: Optional[int] = None):
    await _cmd_shuffle_all(interaction, limit)

# ────────────────────────────────────────────────────────────────────────────
# Запуск
# ────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    if not tele_client.is_connected():
        await tele_client.connect()
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
    async def run():
        await tele_client.connect()
        await main()
    try:
        asyncio.run(run())
    finally:
        try:
            if tele_client.is_connected():
                asyncio.run(tele_client.disconnect())
        except RuntimeError:
            pass
