import discord
from discord import app_commands
from discord.ext import commands, tasks
import csv
import mafic
from google import genai
from google.genai import types
from bs4 import BeautifulSoup
import base64
import json
import os
import asyncio
import aiohttp
import hashlib
import io
import warnings
import re
import tempfile
import time as time_module
import math
import inspect
from pathlib import Path
from PIL import Image
from datetime import datetime, timedelta, timezone
import glob as glob_module
import unicodedata
from xml.sax.saxutils import escape as xml_escape

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

try:
    from openpyxl import load_workbook
except Exception:
    load_workbook = None

warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.genai._api_client")


def is_enabled(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


ENABLE_UVLOOP = os.environ.get("GEMINI_ENABLE_UVLOOP", "0") == "1"
if ENABLE_UVLOOP:
    try:
        import uvloop
    except Exception:
        uvloop = None
    else:
        uvloop.install()
else:
    uvloop = None

if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus('libopus.so.0')
    except OSError:
        discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')

# ================= 1. 설정 및 상수 =================
DISCORD_TOKEN = os.environ["GEMINI_DISCORD_TOKEN"]
GOOGLE_API_KEY = os.environ["GEMINI_API_KEY"]

try:
    CLIENT = genai.Client(api_key=GOOGLE_API_KEY)
except Exception as e:
    print(f"[ERROR] Google Client 초기화 실패: {e}")
    CLIENT = None

AVAILABLE_MODELS = {
    # Pro (3 levels: low / medium / high — minimal 미지원)
    "pro-low": {
        "text": "gemini-pro-latest",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "low",
    },
    "pro-medium": {
        "text": "gemini-pro-latest",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "medium",
    },
    "pro-high": {
        "text": "gemini-pro-latest",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "high",
    },
    # Gemini 3.6 Flash (4 levels)
    "flash-minimal": {
        "text": "gemini-3.6-flash",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "minimal",
    },
    "flash-low": {
        "text": "gemini-3.6-flash",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "low",
    },
    "flash-medium": {
        "text": "gemini-3.6-flash",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "medium",
    },
    "flash-high": {
        "text": "gemini-3.6-flash",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "high",
    },
    # Gemini 3.5 Flash-Lite (4 levels)
    "flash-lite-minimal": {
        "text": "gemini-3.5-flash-lite",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "minimal",
    },
    "flash-lite-low": {
        "text": "gemini-3.5-flash-lite",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "low",
    },
    "flash-lite-medium": {
        "text": "gemini-3.5-flash-lite",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "medium",
    },
    "flash-lite-high": {
        "text": "gemini-3.5-flash-lite",
        "image": "gemini-3.1-flash-image",
        "thinking_level": "high",
    },
}
LEGACY_TEXT_MODEL_ALIASES = {
    "gemini-flash-latest": "gemini-3.6-flash",
    "gemini-flash-lite-latest": "gemini-3.5-flash-lite",
}
TEXT_MODEL = AVAILABLE_MODELS["pro-high"]["text"]
IMAGE_MODEL = AVAILABLE_MODELS["pro-high"]["image"]
# Deep Research는 모델 선택과 분리된 전용 명령어(/deepo, /deepm)로만 실행
DEEP_RESEARCH_AGENTS = {
    "deepo": "deep-research-preview-04-2026",
    "deepm": "deep-research-max-preview-04-2026",
}
DEEP_RESEARCH_AGENT_IDS = set(DEEP_RESEARCH_AGENTS.values())
_configured_routing_model = os.environ.get("GEMINI_ROUTING_MODEL", AVAILABLE_MODELS["flash-lite-minimal"]["text"])
ROUTING_MODEL = LEGACY_TEXT_MODEL_ALIASES.get(_configured_routing_model, _configured_routing_model)
DEFAULT_ACTION_ROUTER_ENABLED = is_enabled(os.environ.get("GEMINI_ENABLE_ACTION_ROUTER", "1"))
# VIDEO_GEN_MODEL = "veo-3.1-generate-preview"  # 비용 절감으로 임시 비활성화

DEFAULT_HISTORY_LIMIT = 20
MIN_HISTORY_LIMIT = 1
MAX_HISTORY_LIMIT = 100
DEFAULT_IMAGE_HISTORY_LIMIT = int(os.environ.get("GEMINI_IMAGE_HISTORY_LIMIT", "10"))
MIN_IMAGE_HISTORY_LIMIT = 0
MAX_IMAGE_HISTORY_LIMIT = 50
IMAGE_SEARCH_LIMIT = 2
MAX_DOC_CHARS = 12000
MEMORY_EXCERPT_MAX_CHARS = 5000
TEXT_ATTACHMENT_EXTENSIONS = (
    ".txt", ".py", ".json", ".md", ".c", ".cpp", ".js", ".html", ".css",
    ".log", ".xml", ".yaml", ".yml", ".toml", ".ini", ".sh", ".csv", ".tsv",
)
MODEL_HISTORY_LIMITS = {
    AVAILABLE_MODELS["pro-high"]["text"]: 20,
    AVAILABLE_MODELS["flash-high"]["text"]: 20,
    AVAILABLE_MODELS["flash-lite-minimal"]["text"]: 20,
}

IMAGE_ACTION_NOUN_RE = re.compile(
    r"(이미지|그림|사진|짤|일러스트|삽화|캐릭터|배경|썸네일|아이콘|로고|아바타|프로필|만화|웹툰|"
    r"\bimage\b|\bpicture\b|\bphoto\b|\billustration\b|\bdrawing\b|\blogo\b|\bicon\b|\bthumbnail\b|\bmeme\b|\bavatar\b|\bwallpaper\b)",
    re.IGNORECASE,
)
IMAGE_ACTION_VERB_RE = re.compile(
    r"(그려|그리|그린|그림으로|이미지로|사진처럼|짤로|생성|만들|제작|렌더|편집|수정|변환|합성|지워|넣어|추가|바꿔|"
    r"\bgenerate\b|\bdraw\b|\bcreate\b|\bmake\b|\brender\b|\bedit\b|\bmodify\b|\btransform\b|\bremove\b|\badd\b)",
    re.IGNORECASE,
)
IMAGE_DRAW_REQUEST_RE = re.compile(
    r"(그려\s*(줘|봐|라|달라|달라고|주라|주세요)?|그리(라고|라|세요|시오|기)|그리(?=\s|$|[.!?,])|"
    r"\bdraw\b|\bsketch\b)",
    re.IGNORECASE,
)
CURRENT_IMAGE_EDIT_RE = re.compile(
    r"(이거|이것|저거|저것|첨부|사진|이미지|그림|짤|reference|attached|this|that).{0,40}"
    r"(편집|수정|변환|합성|지워|넣어|추가|바꿔|스타일|풍으로|배경|edit|modify|transform|remove|add|style|background)",
    re.IGNORECASE,
)
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
FOLLOWUP_IMAGE_EDIT_RE = re.compile(
    r"(이거|이것|이게|저거|저것|그거|그것|아까|방금|전에|위에|최근|this|that|previous|recent|above).{0,80}"
    r"(만들|그려|그리|바꿔|변환|수정|편집|합성|처럼|같이|느낌|스타일|풍으로|make|draw|turn|change|edit|style)",
    re.IGNORECASE,
)
RECENT_IMAGE_INSPECTION_RE = re.compile(
    r"((이미지|그림|사진|짤|첨부|캡처|스크린샷|파일|제목|image|picture|photo|attachment|screenshot|title).{0,60}"
    r"(봐|봐봐|보라|보라고|보여|볼\s*수|확인|분석|읽|제목|수상|이상|see|look|view|inspect|analy[sz]e|read|title|weird)|"
    r"(봐|봐봐|보라|보라고|보여|확인|분석|읽|제목|수상|이상|see|look|view|inspect|analy[sz]e|read|title|weird).{0,60}"
    r"(이미지|그림|사진|짤|첨부|캡처|스크린샷|파일|제목|image|picture|photo|attachment|screenshot|title))",
    re.IGNORECASE,
)
RECENT_IMAGE_REFERENCE_RE = re.compile(
    r"((아까|방금|전에|이전|최근|위에|저번|earlier|previous|recent|above|last).{0,40}"
    r"(이미지|그림|사진|짤|첨부|캡처|스크린샷|image|picture|photo|attachment|screenshot)|"
    r"(이미지|그림|사진|짤|첨부|캡처|스크린샷|image|picture|photo|attachment|screenshot).{0,40}"
    r"(아까|방금|전에|이전|최근|위에|저번|earlier|previous|recent|above|last))",
    re.IGNORECASE,
)

_COMMON_RULES = """## General Rules
- Output response content directly. No prefixes like 'Bot:' or 'Model:'.
- Respond in the user's language and mirror their tone (casual, formal, vulgar — match them).
- If the user sets up a specific roleplay scenario (character, world, setting), maintain it consistently within that conversation. Do NOT treat casual chat as "established context" that must persist.
- If you see `[System Error]` or `[Error]` in history, YOUR previous action failed.
- When a URL is in the message, the system fetches it and attaches `[Webpage Content]` — use that content directly.
- Analyze attached files (images/video/audio) when supported.
- Keep responses concise and Discord-appropriate.
- Never output fake tool-call JSON or fake actions such as `dalle.text2im`. If a tool was not actually called by the app, do not pretend it was.
- Use Google Grounding Search (Web Search) actively since your training cutoff may be outdated."""

SYSTEM_PROMPT = f"""
You are an AI member of this Discord server.

## User Identification
- Each historical user message starts with `[from display_name (id:numeric_id)]`.
- The CURRENT message you should respond to starts with `[CURRENT MESSAGE from display_name (id:numeric_id)]` — focus your reply on this user.
- Past bot replies start with `[Bot replied to display_name (id:numeric_id)]`, indicating which user that reply addressed.
- The numeric ID is a UNIQUE identifier. Two users with similar-looking names are ALWAYS different people if their IDs differ.
- NEVER merge or conflate messages from different IDs, even if topics are similar or names look alike.
- Track each user's statements separately. What User A said is not what User B said.

## Conversation Context
- Chat history is for CONTEXT ONLY — to understand the flow, not to recycle.
- When the topic changes, focus ONLY on the current topic. Do NOT drag in old topics.
- Do NOT reuse specific words, jokes, memes, or references from previous conversations to fake familiarity. Every new topic gets a fresh start.
- User's background info (job, interests, etc.) is for matching TONE only. Do NOT shoehorn it into answers unless the user explicitly brings it up or it has direct logical relevance to the current question.
- Answer new questions cleanly and on-point. Do NOT over-decorate answers by mixing in past context.
- You may briefly reference a previous topic AT MOST once if naturally relevant, then move on entirely.

{_COMMON_RULES}
"""

CHAT_SYSTEM_PROMPT = f"""
You are a helpful AI assistant.
{_COMMON_RULES}
- If an image is provided, analyze it carefully and respond based on both the image and the user's message.
"""

IMAGE_SYSTEM_PROMPT = f"""
You are an AI image generator assistant.
{_COMMON_RULES}
- Return a detailed English image prompt based on the user's request.
- The `message` field may contain a brief response to the user, or be empty.
- If a reference image is provided, describe the requested edit while preserving relevant source details.
"""

# 현재 파일(gemini.py)이 있는 폴더 경로를 구함
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
KST = timezone(timedelta(hours=9))
DOC_CACHE_DIR = os.environ.get("GEMINI_DOC_CACHE_DIR", os.path.join(os.path.dirname(CONFIG_FILE), "gemini_doc_cache"))
DOC_CACHE_AUTO_REPLAY_WINDOW = 2
RECENT_IMAGE_DIR = os.environ.get("GEMINI_RECENT_IMAGE_DIR", os.path.join(os.path.dirname(CONFIG_FILE), "gemini_recent_images"))
RECENT_IMAGE_INDEX_FILE = os.path.join(RECENT_IMAGE_DIR, "index.json")
RECENT_IMAGE_LIMIT = 2

SOUND_DIR = os.path.join(BASE_DIR, "sb")
SOUND_EXTENSIONS = ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.opus', '.weba')
os.makedirs(SOUND_DIR, exist_ok=True)
os.makedirs(os.path.join(SOUND_DIR, "discord"), exist_ok=True)
LAVALINK_PAD_CACHE_DIR = Path(os.environ.get("GEMINI_LAVALINK_PAD_CACHE_DIR", os.path.join(SOUND_DIR, ".gemini_lavalink_pad")))
SOUND_VOLUME = 0.15  # 기본 볼륨 (0.0~1.0)
MIN_SOUND_VOLUME = 0.01
MAX_SOUND_VOLUME = 1.0
SIBO_LATENCY_COMP = 0.0  # Discord 전송 지연 보정 (초)
SIBO_PRELOAD_LEAD = 0.3  # 정각 전 ffmpeg 미리 띄워 파이프 버퍼 채우는 시간 (초)
LAVALINK_POST_CONNECT_GRACE = 0.50  # Lavalink 최초 연결 직후 첫 재생 앞머리 손실 완충
LAVALINK_SHORT_CLIP_PAD_MS = 220
LAVALINK_SIBO_PAD_MS = 120
VOICE_RECONNECT_DELAY = 3.0
VOICE_STATE_REJOIN_DELAY = 8.0
VOICE_RECONNECT_COOLDOWN = 20.0
VOICE_LIBRARY_RECOVERY_GRACE = 75.0
VOICE_HEALTH_GRACE = 30.0
VOICE_HEALTH_BAD_LIMIT = 3
VOICE_HEALTH_RECOVERY = os.environ.get("GEMINI_VOICE_HEALTH_RECOVERY", "0") == "1"
VOICE_SELF_DEAF = True
VOICE_SELF_MUTE = False
VOICE_FORCE_STATE = os.environ.get("GEMINI_VOICE_FORCE_STATE", "0") == "1"
VOICE_SILENCE_KEEPALIVE = os.environ.get("GEMINI_VOICE_SILENCE_KEEPALIVE", "0") == "1"
VOICE_SILENCE_RESUME_DELAY = 0.5
VOICE_DEBUG_LOGS = False
LOOP_MONITOR_INTERVAL = 2.0
LOOP_MONITOR_WARN = 2.5
LAVALINK_ENABLED = os.environ.get("GEMINI_USE_LAVALINK", "0") == "1"
LAVALINK_HOST = os.environ.get("LAVALINK_HOST", "lavalink")
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.environ.get("LAVALINK_PASSWORD", "youshallnotpass")
LAVALINK_SECURE = os.environ.get("LAVALINK_SECURE", "0") == "1"
LAVALINK_LABEL = os.environ.get("LAVALINK_LABEL", "main")
LAVALINK_LOCAL_ROOT = Path(os.environ.get("LAVALINK_LOCAL_ROOT", "/opt/lavalink/sb"))
LAVALINK_TIMEOUT = float(os.environ.get("LAVALINK_TIMEOUT", "10"))

def format_channel_ref(channel):
    if channel is None:
        return "None"
    name = getattr(channel, "name", str(channel))
    channel_id = getattr(channel, "id", None)
    return f"{name}({channel_id})" if channel_id else name

def format_metric(value):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)

def is_lavalink_player(vc):
    return LAVALINK_ENABLED and isinstance(vc, mafic.Player)

def voice_is_connected(vc):
    if vc is None:
        return False
    try:
        return vc.is_connected()
    except Exception:
        return bool(getattr(vc, "connected", False))

def voice_is_playing(vc):
    if vc is None:
        return False
    if is_lavalink_player(vc):
        return getattr(vc, "current", None) is not None and not getattr(vc, "paused", False)
    try:
        return vc.is_playing()
    except Exception:
        return False

def voice_is_paused(vc):
    if vc is None:
        return False
    if is_lavalink_player(vc):
        return bool(getattr(vc, "paused", False))
    try:
        return vc.is_paused()
    except Exception:
        return False

async def voice_stop(vc):
    if vc is None:
        return
    if is_lavalink_player(vc):
        await vc.stop()
        return
    vc.stop()

async def voice_disconnect(vc, *, force=True):
    if vc is None:
        return
    if is_lavalink_player(vc):
        await vc.disconnect(force=force)
        return
    try:
        await vc.disconnect(force=force)
    except TypeError:
        await vc.disconnect()

async def attempt_voice_state_rejoin(guild_id, channel_id):
    await asyncio.sleep(VOICE_STATE_REJOIN_DELAY)

    guild = bot.get_guild(guild_id)
    if guild is None or bot.is_closed():
        return

    if bot.get_voice_target(guild_id) != channel_id:
        return

    vc = guild.voice_client
    if vc and voice_is_connected(vc):
        return

    if not bot.can_retry_voice(guild_id):
        return

    try:
        log_voice_snapshot(
            "voice_state 빠른 재접속 시도",
            guild,
            vc,
            extra=f"target_channel_id={channel_id}",
        )
        vc = await ensure_voice_connection(
            guild,
            channel_id,
            move=True,
            reason="voice_state",
        )
        if vc:
            bot.reset_voice_retry(guild_id)
    except Exception as e:
        log_voice_debug(f"[VC] voice_state 빠른 재접속 실패 ({guild.name}): {e}")

def build_lavalink_local_identifier(filepath):
    relative = Path(filepath).resolve().relative_to(Path(SOUND_DIR).resolve())
    # Lavalink local source expects a filesystem path, not a file:// URI.
    return (LAVALINK_LOCAL_ROOT / relative).as_posix()

def get_voice_ws_closed(vc):
    ws = getattr(vc, "ws", None)
    if ws is None:
        return None
    closed = getattr(ws, "closed", None)
    if callable(closed):
        try:
            return closed()
        except Exception:
            return "error"
    return closed

def get_history_limit_for_text_model(text_model):
    return MODEL_HISTORY_LIMITS.get(text_model, DEFAULT_HISTORY_LIMIT)

def clamp_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_LIMIT
    return max(MIN_HISTORY_LIMIT, min(MAX_HISTORY_LIMIT, limit))

def get_history_limit(config):
    return clamp_history_limit((config or {}).get("history_limit", DEFAULT_HISTORY_LIMIT))


def clamp_image_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_IMAGE_HISTORY_LIMIT
    return max(MIN_IMAGE_HISTORY_LIMIT, min(MAX_IMAGE_HISTORY_LIMIT, limit))


def get_image_history_limit(config):
    return clamp_image_history_limit((config or {}).get("image_history_limit", DEFAULT_IMAGE_HISTORY_LIMIT))


def clamp_sound_volume(value):
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return SOUND_VOLUME
    if volume > 1:
        volume /= 100.0
    return max(MIN_SOUND_VOLUME, min(MAX_SOUND_VOLUME, volume))


def get_voice_unhealthy_reason(vc):
    if vc is None:
        return None
    if is_lavalink_player(vc):
        return None
    reasons = []
    latency = getattr(vc, "latency", None)
    average_latency = getattr(vc, "average_latency", None)
    if isinstance(latency, float) and not math.isfinite(latency):
        reasons.append(f"latency={latency}")
    if isinstance(average_latency, float) and not math.isfinite(average_latency):
        reasons.append(f"avg_latency={average_latency}")
    ws_closed = get_voice_ws_closed(vc)
    if ws_closed is True:
        reasons.append("voice_ws_closed=True")
    endpoint = getattr(vc, "endpoint", None)
    if not endpoint:
        reasons.append("endpoint=missing")
    return ", ".join(reasons) if reasons else None

def describe_voice_client(vc):
    if vc is None:
        return "vc=None"
    if is_lavalink_player(vc):
        return (
            f"connected={voice_is_connected(vc)} "
            f"playing={voice_is_playing(vc)} "
            f"paused={voice_is_paused(vc)} "
            f"channel={format_channel_ref(getattr(vc, 'channel', None))} "
            f"node={getattr(getattr(vc, 'node', None), 'label', '-') } "
            f"ping={format_metric(getattr(vc, 'ping', None))} "
            f"current={getattr(getattr(vc, 'current', None), 'identifier', '-')}"
        )
    try:
        connected = vc.is_connected()
    except Exception as e:
        connected = f"err:{e.__class__.__name__}"
    try:
        playing = vc.is_playing()
    except Exception as e:
        playing = f"err:{e.__class__.__name__}"
    try:
        paused = vc.is_paused()
    except Exception as e:
        paused = f"err:{e.__class__.__name__}"
    endpoint = getattr(vc, "endpoint", None) or "-"
    session_id = getattr(vc, "session_id", None) or "-"
    ws_closed = get_voice_ws_closed(vc)
    return (
        f"connected={connected} playing={playing} paused={paused} "
        f"channel={format_channel_ref(getattr(vc, 'channel', None))} "
        f"endpoint={endpoint} session_id={session_id} "
        f"latency={format_metric(getattr(vc, 'latency', None))} "
        f"avg_latency={format_metric(getattr(vc, 'average_latency', None))} "
        f"ws_closed={ws_closed}"
    )

def describe_voice_state(state):
    if state is None:
        return "state=None"
    return (
        f"channel={format_channel_ref(getattr(state, 'channel', None))} "
        f"self_mute={getattr(state, 'self_mute', None)} "
        f"self_deaf={getattr(state, 'self_deaf', None)} "
        f"mute={getattr(state, 'mute', None)} "
        f"deaf={getattr(state, 'deaf', None)} "
        f"suppress={getattr(state, 'suppress', None)} "
        f"afk={getattr(state, 'afk', None)}"
    )

def log_voice_snapshot(prefix, guild=None, vc=None, extra=None):
    if not VOICE_DEBUG_LOGS:
        return
    parts = [f"[VC] {prefix}"]
    if guild is not None:
        parts.append(f"guild={guild.name}({guild.id})")
    if extra:
        parts.append(extra)
    parts.append(describe_voice_client(vc))
    print(" | ".join(parts))

def log_voice_debug(message):
    if VOICE_DEBUG_LOGS:
        print(message)

async def cleanup_stale_voice_client(vc, guild):
    """끊어진 VoiceClient 캐시를 정리해서 재접속 막힘 방지."""
    log_voice_snapshot("stale VC 정리 시작", guild, vc)
    try:
        await voice_disconnect(vc, force=True)
    except Exception as e:
        log_voice_debug(f"[VC] stale disconnect 실패 ({guild.name}): {e}")
    try:
        vc.cleanup()
    except Exception:
        pass
    log_voice_snapshot("stale VC 정리 완료", guild, guild.voice_client)

async def resolve_voice_channel(guild, channel_id):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            log_voice_debug(f"[VC] 채널 조회 실패 ({guild.name}, {channel_id}): {e}")
            return None
    if not hasattr(channel, "connect"):
        log_voice_debug(f"[VC] 연결 불가 채널 타입 ({guild.name}, {channel_id})")
        return None
    return channel

async def apply_voice_preferences(guild, channel):
    if LAVALINK_ENABLED:
        return
    if not VOICE_FORCE_STATE:
        return
    me = guild.me or guild.get_member(bot.user.id)
    current_state = getattr(me, "voice", None) if me else None
    if (
        current_state
        and current_state.channel == channel
        and current_state.self_deaf == VOICE_SELF_DEAF
        and current_state.self_mute == VOICE_SELF_MUTE
    ):
        return
    try:
        await guild.change_voice_state(
            channel=channel,
            self_deaf=VOICE_SELF_DEAF,
            self_mute=VOICE_SELF_MUTE,
        )
    except Exception as e:
        log_voice_debug(f"[VC] voice state 선호값 적용 실패 ({guild.name}): {e}")

def make_silence_audio_source():
    return discord.FFmpegPCMAudio(
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        before_options="-f lavfi",
        options="-vn",
    )

async def ensure_silence_keepalive(guild, vc=None, *, reason="unknown"):
    if LAVALINK_ENABLED:
        return False
    if not VOICE_SILENCE_KEEPALIVE:
        return False

    target_channel_id = bot.get_voice_target(guild.id)
    if not target_channel_id:
        return False

    if vc is None:
        vc = guild.voice_client

    if vc is None or not voice_is_connected(vc):
        return False

    try:
        if voice_is_playing(vc) or voice_is_paused(vc):
            return False
    except Exception:
        return False

    try:
        vc.play(make_silence_audio_source())
        log_voice_debug(f"[VC] 무음 keepalive 시작 ({guild.name}) [{reason}]")
        return True
    except Exception as e:
        log_voice_debug(f"[VC] 무음 keepalive 시작 실패 ({guild.name}) [{reason}]: {e}")
        return False

async def resume_silence_keepalive(guild_id, delay=VOICE_SILENCE_RESUME_DELAY, *, reason="unknown"):
    if not VOICE_SILENCE_KEEPALIVE:
        return

    if delay > 0:
        await asyncio.sleep(delay)

    guild = bot.get_guild(guild_id)
    if guild is None or bot.is_closed():
        return

    target_channel_id = bot.get_voice_target(guild.id)
    if not target_channel_id:
        return

    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        vc = await ensure_voice_connection(
            guild,
            target_channel_id,
            move=True,
            reason=f"{reason}_resume",
        )
        if vc is None:
            return

    await ensure_silence_keepalive(guild, vc, reason=reason)

def schedule_silence_keepalive_resume(guild_id, delay=VOICE_SILENCE_RESUME_DELAY, *, reason="unknown"):
    if not VOICE_SILENCE_KEEPALIVE or bot.is_closed():
        return

    try:
        asyncio.run_coroutine_threadsafe(
            resume_silence_keepalive(guild_id, delay=delay, reason=reason),
            bot.loop,
        )
    except Exception as e:
        guild = bot.get_guild(guild_id)
        guild_name = guild.name if guild else guild_id
        log_voice_debug(f"[VC] 무음 keepalive 재개 예약 실패 ({guild_name}) [{reason}]: {e}")

async def ensure_voice_connection(guild, channel_id, *, move=True, reason="unknown"):
    channel = await resolve_voice_channel(guild, channel_id)
    if channel is None:
        log_voice_debug(f"[VC] 연결 대상 채널 조회 실패 ({guild.name}, {channel_id}) [{reason}]")
        return None

    async with bot.get_voice_lock(guild.id):
        vc = guild.voice_client
        log_voice_snapshot(
            f"ensure_voice_connection 시작 [{reason}]",
            guild,
            vc,
            extra=f"target={format_channel_ref(channel)} move={move}",
        )
        if vc and voice_is_connected(vc):
            if move and vc.channel != channel:
                old_channel = vc.channel
                if is_lavalink_player(vc):
                    await vc.disconnect(force=True)
                    vc = await channel.connect(
                        cls=mafic.Player,
                        timeout=LAVALINK_TIMEOUT,
                        reconnect=True,
                        self_deaf=VOICE_SELF_DEAF,
                        self_mute=VOICE_SELF_MUTE,
                    )
                else:
                    await vc.move_to(channel)
                log_voice_snapshot(
                    f"이동 완료 [{reason}]",
                    guild,
                    vc,
                    extra=f"from={format_channel_ref(old_channel)} to={format_channel_ref(channel)}",
                )
            await apply_voice_preferences(guild, channel)
            bot.mark_voice_connected(guild.id)
            await ensure_silence_keepalive(guild, vc, reason=reason)
            return vc

        if vc:
            await cleanup_stale_voice_client(vc, guild)

        if LAVALINK_ENABLED:
            await bot.ensure_lavalink_node()
            vc = await channel.connect(
                cls=mafic.Player,
                timeout=LAVALINK_TIMEOUT,
                reconnect=True,
                self_deaf=VOICE_SELF_DEAF,
                self_mute=VOICE_SELF_MUTE,
            )
        else:
            try:
                connect_kwargs = {"reconnect": True}
                if VOICE_FORCE_STATE:
                    connect_kwargs.update(
                        self_deaf=VOICE_SELF_DEAF,
                        self_mute=VOICE_SELF_MUTE,
                    )
                vc = await channel.connect(**connect_kwargs)
            except TypeError:
                vc = await channel.connect()

        await apply_voice_preferences(guild, channel)
        log_voice_snapshot(
            f"연결 완료 [{reason}]",
            guild,
            vc,
            extra=f"target={format_channel_ref(channel)}",
        )
        bot.mark_voice_connected(guild.id)
        await ensure_silence_keepalive(guild, vc, reason=reason)
        return vc

# ================= 사운드보드 유틸리티 =================

def scan_sound_files():
    """재귀 스캔. 키: 상대경로(확장자 제외, 소문자). 값: 절대경로."""
    sounds = {}
    for root, dirs, files in os.walk(SOUND_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for f in files:
            if f.startswith("."):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in SOUND_EXTENSIONS:
                fullpath = os.path.join(root, f)
                relpath = os.path.relpath(fullpath, SOUND_DIR)
                key = os.path.splitext(relpath)[0].lower()
                key = key.replace("\\", "/")
                # macOS NFD → NFC 정규화 (한글 호환)
                key = unicodedata.normalize("NFC", key)
                sounds[key] = fullpath
    return sounds

async def temp_reply(interaction, content, delay=5):
    """메시지 보내고 delay초 후 자동 삭제."""
    msg = await interaction.response.send_message(content)
    actual = await interaction.original_response()
    await asyncio.sleep(delay)
    try:
        await actual.delete()
    except Exception:
        pass

async def temp_followup(interaction, content, delay=5):
    """followup 메시지 보내고 delay초 후 자동 삭제."""
    msg = await interaction.followup.send(content)
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def ensure_lavalink_lead_in(filepath, lead_in_ms):
    if lead_in_ms <= 0:
        return filepath
    src = Path(filepath)
    cache_dir = LAVALINK_PAD_CACHE_DIR
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[LAVALINK] 선행 무음 패딩 캐시 생성 실패: {cache_dir}: {e}")
        return filepath
    file_stamp = f"{src.resolve()}::{src.stat().st_mtime_ns}::{lead_in_ms}"
    digest = hashlib.sha1(file_stamp.encode("utf-8")).hexdigest()[:16]
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", unicodedata.normalize("NFC", src.stem)).strip("._") or "sound"
    output_path = cache_dir / f"{safe_stem}_lead_{lead_in_ms}_{digest}.wav"
    if output_path.exists():
        return str(output_path)

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-af",
        f"adelay={lead_in_ms}:all=1",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip()
        print(f"[LAVALINK] 선행 무음 패딩 생성 실패: {err}")
        return filepath
    return str(output_path)


async def play_lavalink_local_sound(player, filepath, volume, *, replace=True, lead_in_ms=0):
    guild = getattr(player, "guild", None)
    if guild is not None:
        connected_age = bot.get_voice_connected_age(guild.id)
        if connected_age is not None and connected_age < LAVALINK_POST_CONNECT_GRACE:
            await asyncio.sleep(LAVALINK_POST_CONNECT_GRACE - connected_age)
    playback_path = await ensure_lavalink_lead_in(filepath, lead_in_ms)
    identifier = build_lavalink_local_identifier(playback_path)
    await player.play(
        identifier,
        volume=max(0, min(1000, int(volume * 100))),
        replace=replace,
    )

async def play_sound_in_vc(guild, member, sound_name, bot_instance):
    if not member.voice or not member.voice.channel:
        return False, "음성 채널에 먼저 접속해주세요."

    key = unicodedata.normalize("NFC", sound_name.lower())
    if key not in bot_instance.sound_files:
        available = ', '.join(sorted(bot_instance.sound_files.keys()))
        return False, f"'{sound_name}' 사운드를 찾을 수 없습니다. 사용 가능: {available}"

    filepath = bot_instance.sound_files[key]
    bot_instance.set_voice_target(guild.id, member.voice.channel.id)
    log_voice_snapshot(
        "사운드 재생 요청",
        guild,
        guild.voice_client,
        extra=f"user={member.display_name} sound={key} target={format_channel_ref(member.voice.channel)}",
    )
    vc = await ensure_voice_connection(
        guild,
        member.voice.channel.id,
        move=True,
        reason="soundboard",
    )
    if vc is None:
        return False, "음성 채널 연결에 실패했습니다."

    if voice_is_playing(vc):
        await voice_stop(vc)

    if is_lavalink_player(vc):
        await play_lavalink_local_sound(
            vc,
            filepath,
            bot_instance.sound_volume,
            replace=True,
            lead_in_ms=LAVALINK_SHORT_CLIP_PAD_MS,
        )
        return True, f"**{sound_name}** 재생 중 (Lavalink, vol {int(bot_instance.sound_volume * 100)}%)"

    audio = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, options="-af loudnorm=I=-16:TP=-1.5:LRA=11"),
        volume=bot_instance.sound_volume
    )

    def after_play(error):
        status = f"error={error}" if error else "ok"
        log_voice_snapshot(
            f"사운드 재생 종료 [{key}]",
            guild,
            vc,
            extra=f"status={status}",
        )
        schedule_silence_keepalive_resume(
            guild.id,
            reason=f"soundboard:{key}",
        )

    vc.play(audio, after=after_play)
    return True, f"**{sound_name}** 재생 중 (vol {int(bot_instance.sound_volume * 100)}%)"

# ================= 2. 관리자 클래스 =================
class DataManager:
    @staticmethod
    def load_json(filename, default_data):
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
            except Exception as e:
                print(f"[WARN] JSON 로드 실패: {filename}: {e}")
        return default_data

    @staticmethod
    def save_json(filename, data):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
            tmp_filename = f"{filename}.tmp"
            with open(tmp_filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_filename, filename)
        except Exception as e:
            print(f"[WARN] JSON 저장 실패: {filename}: {e}")


IMAGE_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["respond_text", "inspect_recent_images", "generate_image"],
        },
        "refined_prompt": {"type": "string"},
        "use_recent_images": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["action", "refined_prompt", "use_recent_images", "reason"],
}

IMAGE_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["prompt"],
}


class RecentImageStore:
    def __init__(self, base_dir, index_file, *, limit=2):
        self.base_dir = base_dir
        self.index_file = index_file
        self.limit = limit
        os.makedirs(self.base_dir, exist_ok=True)
        self.index = DataManager.load_json(self.index_file, {})

    def _save(self):
        DataManager.save_json(self.index_file, self.index)

    def _entries(self, guild_id):
        gid = str(guild_id)
        entries = self.index.get(gid, [])
        if not isinstance(entries, list):
            entries = []
        self.index[gid] = entries
        return entries

    def register_bytes(self, guild_id, *, channel_id, message_id, author_id, display_name, filename, image_bytes, source_kind):
        gid = str(guild_id)
        guild_dir = os.path.join(self.base_dir, gid)
        os.makedirs(guild_dir, exist_ok=True)
        ext = os.path.splitext(filename or "")[1].lower() or ".png"
        safe_kind = re.sub(r"[^a-z0-9_]+", "_", (source_kind or "image").lower())
        safe_name = re.sub(r"[^a-z0-9_]+", "_", Path(filename or "image").stem.lower()).strip("_") or "image"
        output_name = f"{int(message_id)}_{safe_kind}_{safe_name}{ext}"
        output_path = os.path.join(guild_dir, output_name)
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        entries = [entry for entry in self._entries(guild_id) if os.path.exists(entry.get("path", ""))]
        entries = [entry for entry in entries if entry.get("path") != output_path]
        entries.append(
            {
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "message_id": int(message_id),
                "author_id": int(author_id),
                "display_name": display_name or str(author_id),
                "filename": filename or output_name,
                "path": output_path,
                "source_kind": source_kind,
                "created_at": datetime.now(KST).isoformat(),
            }
        )
        while len(entries) > self.limit:
            old = entries.pop(0)
            try:
                os.remove(old.get("path", ""))
            except OSError:
                pass
        self.index[gid] = entries
        self._save()

    def list_recent(self, guild_id, *, channel_id=None):
        gid = str(guild_id)
        entries = [entry for entry in self._entries(guild_id) if os.path.exists(entry.get("path", ""))]
        self.index[gid] = entries
        if channel_id is not None:
            entries = [entry for entry in entries if str(entry.get("channel_id")) == str(channel_id)]
        return entries[-self.limit:]

    def describe_recent(self, guild_id, *, channel_id=None):
        entries = self.list_recent(guild_id, channel_id=channel_id)
        if not entries:
            return "No recent images are stored."
        return "\n".join(
            f"{idx}. {entry.get('source_kind', 'image')} | {entry.get('display_name', 'unknown')} | {entry.get('filename', 'image')}"
            for idx, entry in enumerate(reversed(entries), start=1)
        )

class DocumentCache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.docs_dir = os.path.join(cache_dir, "docs")
        self.index_file = os.path.join(cache_dir, "index.json")
        os.makedirs(self.docs_dir, exist_ok=True)
        self.index = self._load_index()

    def _default_index(self):
        return {"version": 1, "next_id": 1, "docs": {}, "message_to_docs": {}}

    def _load_index(self):
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("next_id", 1)
                    data.setdefault("docs", {})
                    data.setdefault("message_to_docs", {})
                    return data
            except Exception:
                pass
        return self._default_index()

    def _save_index(self):
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, indent=2, ensure_ascii=False)

    def _make_doc_id(self):
        doc_id = f"doc_{self.index['next_id']:06d}"
        self.index["next_id"] += 1
        return doc_id

    def _doc_path(self, doc_id):
        return os.path.join(self.docs_dir, f"{doc_id}.txt")

    def _write_doc(self, doc_id, content):
        with open(self._doc_path(doc_id), 'w', encoding='utf-8') as f:
            f.write(content)

    def store_document(self, doc_type, source_label, content, message_id, channel_id):
        message_key = str(message_id)
        docs_for_message = self.index["message_to_docs"].setdefault(message_key, [])
        now = datetime.now(timezone.utc).isoformat()

        for doc_id in docs_for_message:
            doc = self.index["docs"].get(doc_id)
            if doc and doc.get("type") == doc_type and doc.get("source_label") == source_label:
                self._write_doc(doc_id, content)
                doc.update({
                    "channel_id": str(channel_id),
                    "updated_at": now,
                    "size": len(content),
                })
                self._save_index()
                return doc_id

        doc_id = self._make_doc_id()
        self._write_doc(doc_id, content)
        self.index["docs"][doc_id] = {
            "id": doc_id,
            "type": doc_type,
            "source_label": source_label,
            "message_id": message_key,
            "channel_id": str(channel_id),
            "created_at": now,
            "updated_at": now,
            "size": len(content),
        }
        docs_for_message.append(doc_id)
        self._save_index()
        return doc_id

    def get_docs_for_message(self, message_id):
        message_key = str(message_id)
        result = []
        for doc_id in self.index["message_to_docs"].get(message_key, []):
            doc = self.index["docs"].get(doc_id)
            if doc:
                result.append(doc)
        return result

    def has_source(self, message_id, doc_type, source_label):
        for doc in self.get_docs_for_message(message_id):
            if doc.get("type") == doc_type and doc.get("source_label") == source_label:
                return True
        return False

    def get_content(self, doc_id):
        path = self._doc_path(doc_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return None

    def render_blocks(self, message_id, full_content=False):
        blocks = []
        for doc in self.get_docs_for_message(message_id):
            doc_id = doc["id"]
            label = doc.get("source_label", "")
            if full_content:
                content = self.get_content(doc_id)
                if content is None:
                    continue
                title = "Cached Webpage Content" if doc.get("type") == "webpage" else "Cached File Content"
                blocks.append(f"[{title}: {doc_id} | {label}]\n```\n{content}\n```")
            else:
                title = "Cached Webpage" if doc.get("type") == "webpage" else "Cached File"
                blocks.append(f"[{title}: {doc_id} | {label}]")
        return blocks


def trim_document_text(text, limit=MAX_DOC_CHARS):
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n...[truncated]..."


def decode_text_bytes(raw_bytes):
    for encoding in ("utf-8", "cp949", "latin1"):
        try:
            return raw_bytes.decode(encoding)
        except Exception:
            continue
    return None


def extract_pdf_text(raw_bytes):
    if PdfReader is None:
        return None
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        parts = []
        for page in reader.pages[:20]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return trim_document_text("\n\n".join(part for part in parts if part.strip()))
    except Exception:
        return None


def extract_docx_text(raw_bytes):
    if DocxDocument is None:
        return None
    try:
        document = DocxDocument(io.BytesIO(raw_bytes))
        paragraphs = [p.text.strip() for p in document.paragraphs if p.text and p.text.strip()]
        return trim_document_text("\n".join(paragraphs))
    except Exception:
        return None


def extract_xlsx_text(raw_bytes):
    if load_workbook is None:
        return None
    try:
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        blocks = []
        for sheet in workbook.worksheets[:5]:
            rows = []
            for row in sheet.iter_rows(max_row=50, values_only=True):
                cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
        workbook.close()
        return trim_document_text("\n\n".join(blocks))
    except Exception:
        return None


def extract_csv_text(raw_bytes, delimiter=","):
    decoded = decode_text_bytes(raw_bytes)
    if not decoded:
        return None
    try:
        reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
        rows = []
        for idx, row in enumerate(reader):
            if idx >= 100:
                break
            cells = [cell.strip() for cell in row if cell and cell.strip()]
            if cells:
                rows.append(" | ".join(cells))
        return trim_document_text("\n".join(rows))
    except Exception:
        return trim_document_text(decoded)


async def extract_attachment_text(att):
    filename = (att.filename or "").lower()
    raw_bytes = await att.read()

    if filename.endswith(".pdf"):
        return extract_pdf_text(raw_bytes), "pdf"
    if filename.endswith(".docx"):
        return extract_docx_text(raw_bytes), "docx"
    if filename.endswith(".xlsx"):
        return extract_xlsx_text(raw_bytes), "xlsx"
    if filename.endswith(".csv"):
        return extract_csv_text(raw_bytes, delimiter=","), "csv"
    if filename.endswith(".tsv"):
        return extract_csv_text(raw_bytes, delimiter="\t"), "tsv"
    if filename.endswith(TEXT_ATTACHMENT_EXTENSIONS):
        decoded = decode_text_bytes(raw_bytes)
        if decoded:
            return trim_document_text(decoded), "text"
    return None, None


def build_recent_image_hint(guild_id, *, channel_id=None):
    recent_desc = bot.recent_image_store.describe_recent(guild_id, channel_id=channel_id)
    return (
        "Recent stored images (actual files can be reattached by the app if you request visual inspection or editing):\n"
        f"{recent_desc}"
    )


def get_bot_display_name():
    user = getattr(bot, "user", None)
    return getattr(user, "display_name", None) or getattr(user, "name", None) or "Bot"


def get_server_display_name(guild, user):
    user_id = getattr(user, "id", None)
    member = guild.get_member(user_id) if guild and user_id is not None else None
    if member is not None:
        return getattr(member, "display_name", None) or getattr(member, "name", None) or str(user_id)
    if guild and isinstance(user, discord.Member) and getattr(user.guild, "id", None) == guild.id:
        return getattr(user, "display_name", None) or getattr(user, "name", None) or str(user_id)
    if guild:
        return str(user_id or "unknown")
    return getattr(user, "display_name", None) or getattr(user, "name", None) or str(user_id or "unknown")


def format_user_identity(guild, user, kind="history"):
    """사용자 식별 마커 생성. kind: 'history' | 'current'"""
    name = get_server_display_name(guild, user)
    uid = str(getattr(user, "id", "unknown"))
    if kind == "current":
        return f"[CURRENT MESSAGE from {name} (id:{uid})]"
    return f"[from {name} (id:{uid})]"


def format_bot_target_marker(display_name, user_id):
    """봇 응답이 어떤 사용자에게 답한 것인지 표시하는 prefix 마커."""
    return f"[Bot replied to {display_name} (id:{user_id})]"


def prepend_user_identity(text, guild, user, kind="history"):
    """사용자 메시지 본문 앞에 식별 마커를 prefix로 붙임."""
    body = (text or "").lstrip()
    marker = format_user_identity(guild, user, kind=kind)
    if not body:
        return marker
    return f"{marker}\n{body}"


def prepend_bot_target(text, display_name, user_id):
    """봇 응답 본문 앞에 응답 대상 사용자 마커를 prefix로 붙임."""
    body = (text or "").lstrip()
    marker = format_bot_target_marker(display_name, user_id)
    if not body:
        return marker
    return f"{marker}\n{body}"


_INTERNAL_IDENTITY_PREFIX_RE = re.compile(
    r"^(?:"
    r"\s*(?:>\s*)?(?:\*\*|__|`)?"
    r"(?:"
    r"\[CURRENT MESSAGE from [^\n]*? \(id:[^)\n]+\)\]|"
    r"\[Bot replied to [^\n]*? \(id:[^)\n]+\)\]|"
    r"\[from [^\n]*? \(id:[^)\n]+\)\]|"
    r"\[사용자:[^\]\n]+\]|"
    r"\[ID:[^\]\n]+\]"
    r")"
    r"(?:\*\*|__|`)?\s*"
    r")+",
    re.IGNORECASE,
)


def clean_assistant_response_text(text):
    if not isinstance(text, str):
        return text
    # 모델이 Markdown을 씌우거나 같은 마커를 여러 번 복사해도 모두 제거한다.
    text = _INTERNAL_IDENTITY_PREFIX_RE.sub("", text)
    # 레거시 footer 형식 호환
    text = re.sub(r"^\s*discord_author=\{[^\n]*\}\s*", "", text)
    text = re.sub(r"\s*discord_author=\{[^\n]*\}\s*$", "", text)
    # 히스토리 이미지 마커 흉내 제거 (모델이 '(Bot sent an image: ...)' 같은 재구성 마커를 응답에 흘리는 경우 방어)
    text = re.sub(r"\(Bot sent an image:[^)\n]*\)", "", text)
    text = re.sub(r"\(Bot generated an image here[^)\n]*\)", "", text)
    text = re.sub(r"\(User attached an image:[^)\n]*\)", "", text)
    text = re.sub(r"\[이미지 첨부됨\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def coerce_json_object(text):
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None

    candidate = text.strip()
    if not candidate:
        return None
    if "```" in candidate:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
        if match:
            candidate = match.group(1).strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start:end + 1]

    try:
        return json.loads(candidate)
    except Exception:
        return None


def build_recent_conversation_excerpt(history_texts, limit=MEMORY_EXCERPT_MAX_CHARS, window=8):
    lines = []
    for text in (history_texts or [])[-window:]:
        cleaned = (text or "").strip()
        if cleaned:
            lines.append(cleaned)
    excerpt = "\n\n".join(lines)
    if len(excerpt) > limit:
        excerpt = "...[earlier context omitted]...\n" + excerpt[-limit:].lstrip()
    return excerpt


def build_contextual_image_prompt(refined_prompt, current_request, history_texts):
    recent_context = build_recent_conversation_excerpt(history_texts, limit=4000)
    parts = [
        "Create an image for the CURRENT Discord request.",
        "Use recent conversation only to resolve references such as 'this', 'that', 'as above', mentioned characters, styles, or scenes.",
        "Do not create a generic educational diagram unless the current request explicitly asks for one.",
        "If the preferred visual prompt is generic or incomplete, make it specific using the current request and recent context.",
    ]
    if recent_context:
        parts.append(f"[Recent Conversation Context]\n{recent_context}")
    parts.append(f"[Current User Request]\n{(current_request or '').strip()}")
    if refined_prompt:
        parts.append(f"[Preferred Visual Prompt]\n{refined_prompt.strip()}")
    return "\n\n".join(parts)


def compact_prompt_for_status(prompt, limit=500):
    prompt = (prompt or "").strip()
    if len(prompt) <= limit:
        return prompt
    return prompt[:limit].rstrip() + "..."


def is_image_attachment(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    filename = (getattr(attachment, "filename", None) or "").lower()
    return content_type.startswith("image/") or filename.endswith(IMAGE_ATTACHMENT_EXTENSIONS)


def is_audio_or_video_attachment(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    return content_type.startswith("video/") or content_type.startswith("audio/")


def attachment_media_type(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    if "/" in content_type:
        return content_type.split("/", 1)[0]
    if is_image_attachment(attachment):
        return "image"
    return "file"


def infer_attachment_mime(attachment, detected_mime=None):
    if detected_mime:
        return detected_mime.split(";", 1)[0].strip()
    content_type = getattr(attachment, "content_type", None)
    if content_type:
        return content_type.split(";", 1)[0].strip()
    filename = (getattr(attachment, "filename", None) or "").lower()
    if filename.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if filename.endswith(".webp"):
        return "image/webp"
    if filename.endswith(".gif"):
        return "image/gif"
    if filename.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def get_structured_helper_model(text_model=None):
    return ROUTING_MODEL


def has_image_generation_signal(text, *, has_current_images=False):
    text = (text or "").strip()
    if not text:
        return False
    if IMAGE_DRAW_REQUEST_RE.search(text):
        return True
    if IMAGE_ACTION_NOUN_RE.search(text) and IMAGE_ACTION_VERB_RE.search(text):
        return True
    if has_current_images and CURRENT_IMAGE_EDIT_RE.search(text):
        return True
    return False


def has_recent_image_reference_signal(text):
    text = (text or "").strip()
    if not text:
        return False
    return bool(RECENT_IMAGE_REFERENCE_RE.search(text))


def has_recent_image_inspection_signal(text):
    text = (text or "").strip()
    if not text:
        return False
    return bool(RECENT_IMAGE_INSPECTION_RE.search(text))


def should_call_action_router(
    text,
    *,
    has_current_images=False,
    has_recent_images=False,
    allow_image_generation=True,
    allow_recent_images=True,
):
    if allow_image_generation and has_image_generation_signal(text, has_current_images=has_current_images):
        return True
    if allow_recent_images and has_recent_images and FOLLOWUP_IMAGE_EDIT_RE.search(text or ""):
        return True
    if allow_recent_images and has_recent_images and has_recent_image_inspection_signal(text):
        return True
    if allow_recent_images and has_recent_images and has_recent_image_reference_signal(text):
        return True
    return False


def build_gemini_system_prompt():
    return SYSTEM_PROMPT


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def get_interactions_api():
    if CLIENT is None:
        return None
    return getattr(getattr(CLIENT, "aio", None), "interactions", None) or getattr(CLIENT, "interactions", None)


def interaction_media_type(mime_type):
    mime_type = (mime_type or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type in {"application/pdf", "text/csv"}:
        return "document"
    return None


def encode_interaction_data(data):
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(data)).decode("ascii")
    return None


def gemini_part_to_interaction_content(part):
    text = getattr(part, "text", None)
    if text:
        return {"type": "text", "text": text}

    inline_data = getattr(part, "inline_data", None)
    if inline_data is not None:
        mime_type = getattr(inline_data, "mime_type", None)
        content_type = interaction_media_type(mime_type)
        data = encode_interaction_data(getattr(inline_data, "data", None))
        if content_type and data:
            return {"type": content_type, "mime_type": mime_type, "data": data}

    file_data = getattr(part, "file_data", None)
    if file_data is not None:
        mime_type = getattr(file_data, "mime_type", None)
        content_type = interaction_media_type(mime_type)
        uri = getattr(file_data, "file_uri", None)
        if content_type and uri:
            return {"type": content_type, "mime_type": mime_type, "uri": uri}
    return None


def gemini_contents_to_interaction_steps(contents):
    steps = []
    for content in contents or []:
        role = getattr(content, "role", None)
        step_type = "model_output" if role == "model" else "user_input"
        blocks = []
        for part in getattr(content, "parts", None) or []:
            block = gemini_part_to_interaction_content(part)
            if block:
                blocks.append(block)
        if blocks:
            steps.append({"type": step_type, "content": blocks})
    return steps


def image_generation_inputs_to_blocks(contents):
    blocks = []
    for item in contents or []:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if isinstance(item, Image.Image):
            image_buffer = io.BytesIO()
            item.save(image_buffer, format="PNG")
            blocks.append(
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": base64.b64encode(image_buffer.getvalue()).decode("ascii"),
                }
            )
    return blocks


async def create_model_interaction(
    *,
    model,
    input_data,
    system_instruction=None,
    tools=None,
    thinking_level=None,
    response_schema=None,
    response_format=None,
    store=False,
):
    interactions_api = get_interactions_api()
    if interactions_api is None:
        raise RuntimeError("현재 google-genai SDK에서 Interactions API를 찾을 수 없습니다. 패키지 업데이트가 필요합니다.")

    kwargs = {"model": model, "input": input_data, "store": store}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction
    if tools:
        kwargs["tools"] = tools
    if thinking_level is not None:
        kwargs["generation_config"] = {"thinking_level": thinking_level}
    if response_schema is not None:
        kwargs["response_format"] = [
            {"type": "text", "mime_type": "application/json", "schema": response_schema}
        ]
    elif response_format is not None:
        kwargs["response_format"] = response_format
    return await maybe_await(interactions_api.create(**kwargs))


async def call_interaction_get(interactions_api, interaction_id):
    try:
        return await maybe_await(interactions_api.get(interaction_id))
    except TypeError:
        return await maybe_await(interactions_api.get(id=interaction_id))


def extract_interaction_text(interaction):
    text = "\n\n".join(
        block["text"] for block in collect_interaction_outputs(interaction) if block["type"] == "text"
    ).strip()
    if text:
        return text
    return (get_interaction_output_value(interaction, "output_text", "") or "").strip()


def get_interaction_usage(interaction):
    usage = get_interaction_output_value(interaction, "usage")
    return int(get_interaction_output_value(usage, "total_tokens", 0) or 0)


def get_interaction_status(interaction):
    return get_interaction_output_value(interaction, "status", "completed") or "completed"


def get_interaction_output_value(output, key, default=None):
    if isinstance(output, dict):
        return output.get(key, default)
    return getattr(output, key, default)


def summarize_interaction_response(interaction):
    """Return a content-free diagnostic summary for failed/empty interactions."""
    steps = get_interaction_output_value(interaction, "steps", None) or []
    step_summaries = []
    for step in steps:
        step_type = str(get_interaction_output_value(step, "type", "unknown"))
        summary = {"type": step_type}

        if step_type == "model_output":
            content = get_interaction_output_value(step, "content", None) or []
            summary["content"] = [
                {
                    "type": str(get_interaction_output_value(item, "type", "unknown")),
                    "text_chars": len(get_interaction_output_value(item, "text", "") or ""),
                }
                for item in content
            ]
            error = get_interaction_output_value(step, "error")
            if error:
                summary["error"] = {
                    "code": get_interaction_output_value(error, "code"),
                    "message": get_interaction_output_value(error, "message"),
                    "details": get_interaction_output_value(error, "details"),
                }
        elif step_type == "thought":
            thought_summary = get_interaction_output_value(step, "summary", None) or []
            summary["summary_chars"] = sum(
                len(get_interaction_output_value(item, "text", "") or "")
                for item in thought_summary
            )
        elif step_type.endswith("_result"):
            is_error = get_interaction_output_value(step, "is_error")
            if is_error is not None:
                summary["is_error"] = bool(is_error)

        step_summaries.append(summary)

    usage = get_interaction_output_value(interaction, "usage")
    usage_summary = {
        key: get_interaction_output_value(usage, key)
        for key in (
            "total_input_tokens",
            "total_output_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
            "total_tokens",
        )
        if get_interaction_output_value(usage, key) is not None
    }
    diagnostic = {
        "id": get_interaction_output_value(interaction, "id"),
        "status": get_interaction_status(interaction),
        "model": get_interaction_output_value(interaction, "model"),
        "steps": step_summaries,
        "usage": usage_summary,
    }
    return json.dumps(diagnostic, ensure_ascii=False, default=str, separators=(",", ":"))


def decode_interaction_image(data):
    if not data:
        return None
    if isinstance(data, bytes):
        return data
    if not isinstance(data, str):
        return None
    payload = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    try:
        return base64.b64decode(payload)
    except Exception:
        return None


def collect_interaction_outputs(interaction):
    """Interaction 응답에서 텍스트/이미지 블록을 순서대로 수집.

    google-genai 2.x의 Interactions API는 결과를 `outputs`가 아니라
    `steps[].content[]`(type=model_output)로 반환한다. thought/tool 스텝은 건너뛰고
    model_output 스텝의 content 블록만 모은다. 구형 `outputs` 응답도 폴백으로 지원."""
    steps = (
        interaction.get("steps")
        if isinstance(interaction, dict)
        else getattr(interaction, "steps", None)
    ) or []
    blocks = []
    for step in steps:
        if get_interaction_output_value(step, "type", "") != "model_output":
            continue
        blocks.extend(get_interaction_output_value(step, "content", None) or [])
    if not blocks:
        blocks = (
            interaction.get("outputs")
            if isinstance(interaction, dict)
            else getattr(interaction, "outputs", None)
        ) or []
    collected = []
    for output in blocks:
        output_type = get_interaction_output_value(output, "type", "")
        text = get_interaction_output_value(output, "text")
        data = get_interaction_output_value(output, "data")
        mime_type = get_interaction_output_value(output, "mime_type") or get_interaction_output_value(output, "mimeType")
        if output_type == "text" and text:
            collected.append({"type": "text", "text": text})
        elif output_type == "image" and data:
            image_bytes = decode_interaction_image(data)
            if image_bytes:
                collected.append({"type": "image", "data": image_bytes, "mime_type": mime_type or "image/png"})
        elif text:
            collected.append({"type": "text", "text": text})
    return collected


def add_markdownish_text(story, text, styles):
    from reportlab.platypus import Paragraph, Spacer

    in_code = False
    code_lines = []

    def flush_code():
        nonlocal code_lines
        if code_lines:
            escaped = xml_escape("\n".join(code_lines)).replace("\n", "<br/>")
            story.append(Paragraph(escaped, styles["Code"]))
            story.append(Spacer(1, 8))
            code_lines = []

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            story.append(Spacer(1, 6))
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(xml_escape(stripped[2:].strip()), styles["Heading1"]))
        elif stripped.startswith("## "):
            story.append(Paragraph(xml_escape(stripped[3:].strip()), styles["Heading2"]))
        elif stripped.startswith("### "):
            story.append(Paragraph(xml_escape(stripped[4:].strip()), styles["Heading3"]))
        elif re.match(r"^[-*]\s+", stripped):
            story.append(Paragraph("• " + xml_escape(re.sub(r"^[-*]\s+", "", stripped)), styles["Body"]))
        elif re.match(r"^\d+[.)]\s+", stripped):
            story.append(Paragraph(xml_escape(stripped), styles["Body"]))
        else:
            story.append(Paragraph(xml_escape(stripped), styles["Body"]))
    flush_code()


DEEP_RESEARCH_PDF_FONT_CANDIDATES = (
    ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf", None),
    ("NotoSansCJK", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("NanumGothic", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", None),
    ("AppleSDGothicNeo", "/System/Library/Fonts/AppleSDGothicNeo.ttc", 0),
    ("AppleGothic", "/System/Library/Fonts/Supplemental/AppleGothic.ttf", None),
)


def register_deep_research_pdf_font(pdfmetrics, TTFont, UnicodeCIDFont):
    for font_name, font_path, subfont_index in DEEP_RESEARCH_PDF_FONT_CANDIDATES:
        if not os.path.exists(font_path):
            continue
        try:
            if font_name not in pdfmetrics.getRegisteredFontNames():
                try:
                    kwargs = {"subfontIndex": subfont_index} if subfont_index is not None else {}
                    pdfmetrics.registerFont(TTFont(font_name, font_path, **kwargs))
                except TypeError:
                    pdfmetrics.registerFont(TTFont(font_name, font_path))
            print(f"[DeepResearch PDF] using font: {font_name} ({font_path})")
            return font_name
        except Exception as e:
            print(f"[DeepResearch PDF] font unavailable: {font_path} ({e})")

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HYGoThic-Medium"))
        print("[DeepResearch PDF] using fallback CID font: HYGoThic-Medium")
        return "HYGoThic-Medium"
    except Exception as e:
        print(f"[DeepResearch PDF] Korean font fallback failed, using Helvetica ({e})")
        return "Helvetica"


def build_deep_research_pdf(outputs, *, agent_id):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as PdfImage
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception as e:
        raise RuntimeError("reportlab 패키지가 설치되어 있지 않습니다.") from e

    body_font = register_deep_research_pdf_font(pdfmetrics, TTFont, UnicodeCIDFont)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Gemini Deep Research Report",
    )
    styles = {
        "Title": ParagraphStyle("Title", fontName=body_font, fontSize=18, leading=24, spaceAfter=14),
        "Meta": ParagraphStyle("Meta", fontName=body_font, fontSize=9, leading=12, textColor="#555555", spaceAfter=12),
        "Heading1": ParagraphStyle("Heading1", fontName=body_font, fontSize=15, leading=20, spaceBefore=10, spaceAfter=6),
        "Heading2": ParagraphStyle("Heading2", fontName=body_font, fontSize=13, leading=18, spaceBefore=8, spaceAfter=5),
        "Heading3": ParagraphStyle("Heading3", fontName=body_font, fontSize=11, leading=16, spaceBefore=6, spaceAfter=4),
        "Body": ParagraphStyle("Body", fontName=body_font, fontSize=9.5, leading=14, spaceAfter=4),
        "Code": ParagraphStyle("Code", fontName=body_font, fontSize=8, leading=11, leftIndent=8, backColor="#f2f2f2", spaceAfter=6),
    }

    story = [
        Paragraph("Gemini Deep Research Report", styles["Title"]),
        Paragraph(
            xml_escape(f"Agent: {agent_id} | Generated: {datetime.now(KST).strftime('%Y-%m-%d %H:%M JST')}"),
            styles["Meta"],
        ),
    ]
    image_index = 1
    max_width = A4[0] - doc.leftMargin - doc.rightMargin
    max_height = 165 * mm

    for output in outputs:
        if output["type"] == "text":
            add_markdownish_text(story, output.get("text", ""), styles)
        elif output["type"] == "image":
            raw = output.get("data")
            if not raw:
                continue
            try:
                image = Image.open(io.BytesIO(raw))
                width, height = image.size
                scale = min(max_width / width, max_height / height, 1.0)
                story.append(Spacer(1, 8))
                story.append(Paragraph(f"Visualization {image_index}", styles["Heading3"]))
                story.append(PdfImage(io.BytesIO(raw), width=width * scale, height=height * scale))
                story.append(Spacer(1, 8))
                image_index += 1
            except Exception as e:
                story.append(Paragraph(xml_escape(f"[Image omitted: {e}]"), styles["Body"]))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def build_deep_research_report(interaction, *, agent_id):
    outputs = collect_interaction_outputs(interaction)
    if not outputs:
        raise RuntimeError("Deep Research outputs가 비어 있습니다.")
    pdf_bytes = build_deep_research_pdf(outputs, agent_id=agent_id)
    text_preview = "\n\n".join(output["text"] for output in outputs if output["type"] == "text").strip()
    image_count = sum(1 for output in outputs if output["type"] == "image")
    return {
        "kind": "deep_research_report",
        "pdf_bytes": pdf_bytes,
        "filename": f"deep_research_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.pdf",
        "summary": (
            f"[Deep Research] 완료했습니다. PDF 레포트를 첨부합니다."
            f"\n텍스트 출력 {len(text_preview):,}자, 이미지 {image_count}개"
        ),
        "text_preview": text_preview[:MEMORY_EXCERPT_MAX_CHARS],
    }


async def call_deep_research(research_topic, agent_id):
    """대화 컨텍스트 없이 입력된 주제만 단독으로 리서치한다."""
    if not CLIENT:
        return "Error: API Client not initialized"
    research_topic = (research_topic or "").strip()
    if not research_topic:
        return "[ERROR] Deep Research에 전달할 텍스트가 없습니다."
    prompt = (
        "Research the topic below and produce a thorough, well-structured report. "
        "Write the report in the same language as the topic (e.g. Korean topic → Korean report).\n\n"
        "## Research Topic\n"
        f"{research_topic}"
    )

    interactions_api = get_interactions_api()
    if interactions_api is None:
        return "[ERROR] 현재 google-genai SDK에서 Interactions API를 찾을 수 없습니다. 패키지 업데이트가 필요합니다."

    try:
        interaction = await maybe_await(
            interactions_api.create(
                input=prompt,
                agent=agent_id,
                background=True,
                agent_config={"type": "deep-research", "thinking_summaries": "auto", "visualization": "auto"},
            )
        )
        interaction_id = getattr(interaction, "id", None) or (interaction.get("id") if isinstance(interaction, dict) else None)
        if not interaction_id:
            return "[ERROR] Deep Research 작업 ID를 받지 못했습니다."

        for _ in range(90):
            await asyncio.sleep(10)
            result = await call_interaction_get(interactions_api, interaction_id)
            status = getattr(result, "status", None) or (result.get("status") if isinstance(result, dict) else None)
            if status == "completed":
                try:
                    return build_deep_research_report(result, agent_id=agent_id)
                except Exception as e:
                    text = extract_interaction_text(result)
                    if text:
                        return f"[ERROR] Deep Research PDF 생성 실패: {e}\n\n{text}"
                    return f"[ERROR] Deep Research PDF 생성 실패: {e}"
            if status in ("failed", "cancelled", "incomplete", "budget_exceeded", "requires_action"):
                error = getattr(result, "error", None) or (result.get("error") if isinstance(result, dict) else None)
                detail = f": {error}" if error else ""
                return f"[ERROR] Deep Research 중단 (status={status}){detail}"
        return "[ERROR] Deep Research 작업이 제한 시간 안에 끝나지 않았습니다."
    except Exception as e:
        return f"[ERROR] Deep Research 호출 실패: {e}"


async def route_message_action(last_user_msg, *, guild_id, channel_id=None, has_current_images=False, text_model=None, recent_context=None):
    if not CLIENT:
        return {
            "action": "respond_text",
            "refined_prompt": "",
            "use_recent_images": False,
            "reason": "client unavailable",
        }
    if text_model is None:
        text_model = TEXT_MODEL
    text_model = get_structured_helper_model(text_model)
    recent_images = bot.recent_image_store.list_recent(guild_id, channel_id=channel_id)
    if recent_images and not has_current_images and has_recent_image_inspection_signal(last_user_msg):
        return {
            "action": "inspect_recent_images",
            "refined_prompt": "",
            "use_recent_images": True,
            "reason": "direct_recent_image_inspection_signal",
        }

    inspect_action_text = ""
    inspect_rule_text = ""
    recent_image_hint = ""
    if recent_images:
        inspect_action_text = "- inspect_recent_images: the app should reattach the stored recent images before answering."
        inspect_rule_text = (
            "- Choose inspect_recent_images only when the user is asking about something visible in a previous image.\n"
            "- `use_recent_images` should be true only when the app should attach the stored recent images as references."
        )
        recent_image_hint = f"{build_recent_image_hint(guild_id, channel_id=channel_id)}\n"

    recent_context = (recent_context or "").strip()
    prompt = f"""
You are a routing planner for a Discord AI bot.
Decide the next action for the assistant.

Available actions:
- respond_text: normal text reply.
{inspect_action_text}
- generate_image: the user wants a new image or wants to edit/transform an image. In this case, write a refined English prompt for the image model.

Rules:
- Do not choose inspect_recent_images if the current message already includes image attachments.
- Do not infer that product names, drinks, people, or objects refer to stored images by themselves.
- Do not choose generate_image unless the user clearly wants image creation or visual transformation/editing.
- If the current message is only a complaint, acknowledgement, or confusion about a previous answer/image, choose respond_text.
- If the current message is a follow-up image request or edit request, use the recent conversation excerpt to resolve the subject/style and write a specific refined prompt.
{inspect_rule_text}

Current message has image attachments: {has_current_images}

{recent_image_hint}

Recent conversation excerpt:
{recent_context or '(empty)'}

User message:
{last_user_msg}
""".strip()

    try:
        interaction = await create_model_interaction(
            model=text_model,
            input_data=prompt,
            response_schema=IMAGE_ACTION_SCHEMA,
        )
        payload = coerce_json_object(extract_interaction_text(interaction))
        if not payload:
            raise ValueError("empty or invalid JSON route response")
        action = payload.get("action", "respond_text")
        if action not in {"respond_text", "inspect_recent_images", "generate_image"}:
            action = "respond_text"
        if action == "inspect_recent_images" and (has_current_images or not recent_images):
            action = "respond_text"
            payload["use_recent_images"] = False
        if (
            action == "generate_image"
            and not payload.get("use_recent_images")
            and not has_current_images
            and recent_images
            and FOLLOWUP_IMAGE_EDIT_RE.search(last_user_msg or "")
        ):
            payload["use_recent_images"] = True
        return {
            "action": action,
            "refined_prompt": (payload.get("refined_prompt") or "").strip(),
            "use_recent_images": bool(payload.get("use_recent_images", False)),
            "reason": (payload.get("reason") or "").strip(),
        }
    except Exception as e:
        print(f"[IMG] 라우팅 판정 실패: {e}")
        is_followup_edit = bool(FOLLOWUP_IMAGE_EDIT_RE.search(last_user_msg or ""))
        if has_image_generation_signal(last_user_msg, has_current_images=has_current_images) or is_followup_edit:
            return {
                "action": "generate_image",
                "refined_prompt": "",
                "use_recent_images": bool(is_followup_edit and recent_images and not has_current_images),
                "reason": "routing failure direct image signal",
            }
        return {
            "action": "respond_text",
            "refined_prompt": "",
            "use_recent_images": False,
            "reason": "routing failure",
        }

# ================= 3. 유틸리티 & API =================
async def download_media(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return io.BytesIO(data), resp.headers.get('Content-Type')
                else:
                    print(f"[WARN] download_media failed: HTTP {resp.status} for {url[:80]}")
    except Exception as e:
        print(f"[WARN] download_media error: {e}")
    return None, None

async def upload_file_api(file_data, mime_type):
    """File API로 대용량 미디어 업로드 후 ACTIVE 상태까지 polling"""
    if not CLIENT: return None
    tmp = None
    try:
        suffix = '.' + mime_type.split('/')[-1] if '/' in mime_type else '.bin'
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(file_data)
        tmp.close()
        uploaded = await CLIENT.aio.files.upload(
            file=Path(tmp.name),
            config=types.UploadFileConfig(mime_type=mime_type)
        )
        for _ in range(30):  # 최대 60초
            if uploaded.state and uploaded.state.name == "ACTIVE":
                return uploaded
            await asyncio.sleep(2)
            uploaded = await CLIENT.aio.files.get(name=uploaded.name)
        print(f"[WARN] File API 업로드 타임아웃: {mime_type}")
        return None
    except Exception as e:
        print(f"[ERROR] File API 업로드 실패: {e}")
        return None
    finally:
        if tmp:
            try: os.unlink(tmp.name)
            except: pass

async def scrap_webpage(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    for script in soup(["script", "style", "nav", "footer", "iframe"]):
                        script.extract()
                    text = soup.get_text(separator='\n')
                    clean_text = '\n'.join([line.strip() for line in text.splitlines() if line.strip()])
                    return clean_text
    except Exception as e:
        print(f"[WARN] URL 읽기 실패: {e}")
    return None

async def send_long_message(channel, content):
    if not content: return
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        await channel.send(chunk)

async def call_nano_banana(contents, image_model=None):
    if not CLIENT: return None
    if image_model is None:
        image_model = IMAGE_MODEL
    max_retries = 5

    for attempt in range(max_retries):
        try:
            input_blocks = image_generation_inputs_to_blocks(contents)
            if not input_blocks:
                raise ValueError("이미지 생성 입력이 비어 있습니다.")
            coro = create_model_interaction(
                model=image_model,
                input_data=input_blocks,
                response_format={"type": "image"},
            )
            interaction = await asyncio.wait_for(coro, timeout=90.0)
            status = get_interaction_status(interaction)
            if status not in {"completed", "incomplete"}:
                raise Exception(f"Image interaction status: {status}")

            for output in collect_interaction_outputs(interaction):
                if output["type"] == "image":
                    return output["data"]

            output_image = get_interaction_output_value(interaction, "output_image")
            image_data = decode_interaction_image(get_interaction_output_value(output_image, "data"))
            if image_data:
                return image_data

            raise Exception("No image data found in response")

        except Exception as e:
            error_msg = str(e)
            if "500" in error_msg or "Payload" in error_msg or "Connection" in error_msg or "400" in error_msg or "Timeout" in error_msg:
                print(f"[WARN] 이미지 생성 실패 (시도 {attempt+1}/{max_retries}): {error_msg} -> 5초 후 재시도...")
                await asyncio.sleep(5)
                if attempt == max_retries - 1: raise e
            else:
                raise e

async def call_gemini(history_contents, text_model=None, thinking_level=None):
    if not CLIENT: return "Error: API Client not initialized", 0
    if text_model is None:
        text_model = TEXT_MODEL

    try:
        input_data = gemini_contents_to_interaction_steps(history_contents)
        interaction = None
        for attempt in range(2):
            interaction = await create_model_interaction(
                model=text_model,
                input_data=input_data,
                system_instruction=build_gemini_system_prompt(),
                tools=[{"type": "google_search"}],
                thinking_level=thinking_level,
            )
            status = get_interaction_status(interaction)
            raw_text = extract_interaction_text(interaction)
            if status not in {"completed", "incomplete"} or clean_assistant_response_text(raw_text):
                break
            empty_kind = "marker_only" if raw_text else "empty"
            print(
                f"[WARN] Gemini {empty_kind} response "
                f"(attempt={attempt + 1}/2) {summarize_interaction_response(interaction)}"
            )
        return interaction, get_interaction_usage(interaction)

    except Exception as e:
        return f"Error: {e}", 0

# ================= 4. 봇 메인 로직 =================
class GeminiBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self._migrate_legacy_text_models()
        self._migrate_thinking_budget_to_level()
        self._migrate_deep_research_text_models()
        self.channel_locks = {}
        self.voice_locks = {}
        self.voice_retry_after = {}
        self.voice_connected_at = {}
        self.voice_disconnected_at = {}
        self.voice_bad_state_counts = {}
        self.loop_monitor_expected = None
        self.doc_cache = DocumentCache(DOC_CACHE_DIR)
        self.recent_image_store = RecentImageStore(RECENT_IMAGE_DIR, RECENT_IMAGE_INDEX_FILE, limit=RECENT_IMAGE_LIMIT)
        self.sound_files = scan_sound_files()
        global_cfg = self.get_global_config()
        self.sound_volume = clamp_sound_volume(global_cfg.get("sound_volume", SOUND_VOLUME))
        if global_cfg.get("sound_volume") != self.sound_volume:
            global_cfg["sound_volume"] = self.sound_volume
            self.save_config()
        print(f"[SOUND] 볼륨 로드: {int(self.sound_volume * 100)}% ({CONFIG_FILE})")
        self._sound_dir_mtime = self._get_sound_dir_mtime()
        global SIBO_LATENCY_COMP
        SIBO_LATENCY_COMP = self.server_configs.get("_global", {}).get("sibo_comp", SIBO_LATENCY_COMP)
        self.active_views = []  # View 참조 유지용
        self.mafic_pool = mafic.NodePool(self) if LAVALINK_ENABLED else None
        self.mafic_node = None
        self.mafic_connect_lock = asyncio.Lock()

    def _migrate_legacy_text_models(self):
        """저장된 latest 별칭을 현재 고정 모델 ID로 치환한다."""
        changed = False
        for gid, cfg in self.server_configs.items():
            if not isinstance(cfg, dict):
                continue
            old = cfg.get("text_model")
            new = LEGACY_TEXT_MODEL_ALIASES.get(old)
            if not new:
                continue
            cfg["text_model"] = new
            print(f"[MIGRATE] guild {gid}: text_model {old} → {new}")
            changed = True
        if changed:
            DataManager.save_json(CONFIG_FILE, self.server_configs)

    def _migrate_thinking_budget_to_level(self):
        """레거시 thinking_budget 정수를 신규 thinking_level enum으로 일괄 변환.
        Gemini 3 시리즈는 thinking_level 사용 권장(deprecated thinking_budget 정리)."""
        budget_to_level = {
            0: "minimal",
            2048: "low",
            8192: "medium",
            24576: "high",
            -1: "high",  # dynamic 역할 흡수
        }
        changed = False
        for gid, cfg in self.server_configs.items():
            if not isinstance(cfg, dict):
                continue
            if "thinking_budget" not in cfg:
                continue
            budget = cfg.pop("thinking_budget")
            if "thinking_level" not in cfg:
                level = budget_to_level.get(budget, "minimal")
                cfg["thinking_level"] = level
                print(f"[MIGRATE] guild {gid}: thinking_budget={budget} → thinking_level={level}")
            else:
                print(f"[MIGRATE] guild {gid}: drop legacy thinking_budget={budget} (thinking_level already set)")
            changed = True
        if changed:
            DataManager.save_json(CONFIG_FILE, self.server_configs)

    def _migrate_deep_research_text_models(self):
        """모델 선택 목록에서 제외된 Deep Research가 text_model로 남아있으면 기본 모델로 복구.
        Deep Research는 이제 /deepo·/deepm 명령어 전용."""
        changed = False
        for gid, cfg in self.server_configs.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("text_model") in DEEP_RESEARCH_AGENT_IDS:
                old = cfg["text_model"]
                cfg["text_model"] = TEXT_MODEL
                cfg["image_model"] = IMAGE_MODEL
                print(f"[MIGRATE] guild {gid}: text_model {old} → {TEXT_MODEL} (Deep Research는 /deepo·/deepm 전용)")
                changed = True
        if changed:
            DataManager.save_json(CONFIG_FILE, self.server_configs)

    def _get_sound_dir_mtime(self):
        """SOUND_DIR 전체의 최신 mtime 반환."""
        latest = 0
        for root, dirs, files in os.walk(SOUND_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for name in files + dirs:
                if name.startswith("."):
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(root, name))
                    if mt > latest:
                        latest = mt
                except OSError:
                    pass
            # root 폴더 자체의 mtime도 체크 (파일 추가/삭제 감지)
            try:
                mt = os.path.getmtime(root)
                if mt > latest:
                    latest = mt
            except OSError:
                pass
        return latest

    def get_config(self, guild_id):
        gid = str(guild_id)
        if gid not in self.server_configs:
            self.server_configs[gid] = {
                "channel_id": None,
                "reset_time": None,
                "history_limit": DEFAULT_HISTORY_LIMIT,
                "image_history_limit": DEFAULT_IMAGE_HISTORY_LIMIT,
                "action_router": DEFAULT_ACTION_ROUTER_ENABLED,
            }
        self.server_configs[gid].setdefault("history_limit", DEFAULT_HISTORY_LIMIT)
        self.server_configs[gid].setdefault("image_history_limit", DEFAULT_IMAGE_HISTORY_LIMIT)
        self.server_configs[gid].setdefault("action_router", DEFAULT_ACTION_ROUTER_ENABLED)
        return self.server_configs[gid]

    def get_sibo(self, guild_id):
        return self.get_config(guild_id).get("sibo")

    def set_sibo(self, guild_id, sibo_cfg):
        self.get_config(guild_id)["sibo"] = sibo_cfg
        self.save_config()

    def del_sibo(self, guild_id):
        cfg = self.get_config(guild_id)
        cfg.pop("sibo", None)
        self.save_config()

    def get_all_sibo(self):
        """모든 서버의 sibo 설정을 {gid: sibo_cfg} 딕셔너리로 반환."""
        result = {}
        for gid, cfg in self.server_configs.items():
            if gid.startswith("_") or not isinstance(cfg, dict):
                continue
            sibo = cfg.get("sibo")
            if sibo and isinstance(sibo, dict):
                result[gid] = sibo
        return result

    def get_global_config(self):
        global_cfg = self.server_configs.get("_global")
        if not isinstance(global_cfg, dict):
            global_cfg = {}
            self.server_configs["_global"] = global_cfg
        return global_cfg

    def get_lavalink_session_id(self):
        return self.get_global_config().get("lavalink_session_id")

    def set_lavalink_session_id(self, session_id):
        cfg = self.get_global_config()
        if cfg.get("lavalink_session_id") == session_id:
            return
        cfg["lavalink_session_id"] = session_id
        self.save_config()

    def set_sound_volume(self, volume):
        self.sound_volume = clamp_sound_volume(volume)
        self.get_global_config()["sound_volume"] = self.sound_volume
        self.save_config()
        print(f"[SOUND] 볼륨 저장: {int(self.sound_volume * 100)}% ({CONFIG_FILE})")

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)

    async def ensure_lavalink_node(self):
        if not LAVALINK_ENABLED:
            return None
        async with self.mafic_connect_lock:
            if self.mafic_node is not None:
                return self.mafic_node
            self.mafic_node = await self.mafic_pool.create_node(
                host=LAVALINK_HOST,
                port=LAVALINK_PORT,
                label=LAVALINK_LABEL,
                password=LAVALINK_PASSWORD,
                secure=LAVALINK_SECURE,
                timeout=LAVALINK_TIMEOUT,
                resuming_session_id=self.get_lavalink_session_id(),
                player_cls=mafic.Player,
            )
            if getattr(self.mafic_node, "session_id", None):
                self.set_lavalink_session_id(self.mafic_node.session_id)
            return self.mafic_node

    def get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    def get_voice_lock(self, guild_id):
        gid = str(guild_id)
        if gid not in self.voice_locks:
            self.voice_locks[gid] = asyncio.Lock()
        return self.voice_locks[gid]

    def get_voice_target(self, guild_id):
        cfg = self.get_config(guild_id)
        channel_id = cfg.get("voice_target_channel_id")
        if channel_id:
            return channel_id
        sibo = cfg.get("sibo")
        if isinstance(sibo, dict):
            return sibo.get("vc_channel")
        return None

    def set_voice_target(self, guild_id, channel_id):
        cfg = self.get_config(guild_id)
        if cfg.get("voice_target_channel_id") != channel_id:
            cfg["voice_target_channel_id"] = channel_id
            self.save_config()
            guild = self.get_guild(guild_id)
            guild_name = guild.name if guild else guild_id
            log_voice_debug(f"[VC] keepalive 대상 저장 ({guild_name}): channel_id={channel_id}")

    def clear_voice_target(self, guild_id):
        cfg = self.get_config(guild_id)
        if "voice_target_channel_id" in cfg:
            old_channel_id = cfg.get("voice_target_channel_id")
            cfg.pop("voice_target_channel_id", None)
            self.save_config()
            self.voice_bad_state_counts.pop(str(guild_id), None)
            self.voice_connected_at.pop(str(guild_id), None)
            self.voice_disconnected_at.pop(str(guild_id), None)
            guild = self.get_guild(guild_id)
            guild_name = guild.name if guild else guild_id
            log_voice_debug(f"[VC] keepalive 대상 해제 ({guild_name}): channel_id={old_channel_id}")

    def can_retry_voice(self, guild_id):
        now = time_module.monotonic()
        gid = str(guild_id)
        next_allowed = self.voice_retry_after.get(gid, 0.0)
        if now < next_allowed:
            return False
        self.voice_retry_after[gid] = now + VOICE_RECONNECT_COOLDOWN
        return True

    def reset_voice_retry(self, guild_id):
        self.voice_retry_after[str(guild_id)] = 0.0

    def mark_voice_connected(self, guild_id):
        gid = str(guild_id)
        self.voice_connected_at[gid] = time_module.monotonic()
        self.voice_disconnected_at.pop(gid, None)
        self.voice_bad_state_counts[gid] = 0

    def mark_voice_disconnected(self, guild_id):
        gid = str(guild_id)
        self.voice_connected_at.pop(gid, None)
        self.voice_disconnected_at[gid] = time_module.monotonic()
        self.voice_bad_state_counts[gid] = 0

    def get_voice_disconnected_age(self, guild_id):
        started = self.voice_disconnected_at.get(str(guild_id))
        if started is None:
            return None
        return time_module.monotonic() - started

    def get_voice_connected_age(self, guild_id):
        started = self.voice_connected_at.get(str(guild_id))
        if started is None:
            return None
        return time_module.monotonic() - started

    def record_voice_health(self, guild_id, unhealthy_reason):
        gid = str(guild_id)
        if unhealthy_reason:
            self.voice_bad_state_counts[gid] = self.voice_bad_state_counts.get(gid, 0) + 1
            return self.voice_bad_state_counts[gid]
        self.voice_bad_state_counts[gid] = 0
        return 0

bot = GeminiBot()

@tasks.loop(seconds=10)
async def voice_watchdog():
    """VC 연결 유지 감시: 끊긴 경우 자동 재접속 + 비정상 상태 강제 재연결."""
    for guild in bot.guilds:
        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                target_channel_id = bot.get_voice_target(guild.id)
                if VOICE_HEALTH_RECOVERY:
                    unhealthy_reason = get_voice_unhealthy_reason(vc)
                    connected_age = bot.get_voice_connected_age(guild.id)
                    bad_count = bot.record_voice_health(guild.id, unhealthy_reason)
                    if (
                        unhealthy_reason
                        and target_channel_id
                        and connected_age is not None
                        and connected_age >= VOICE_HEALTH_GRACE
                        and bad_count >= VOICE_HEALTH_BAD_LIMIT
                        and bot.can_retry_voice(guild.id)
                    ):
                        log_voice_snapshot(
                            "watchdog 강제 재연결",
                            guild,
                            vc,
                            extra=(
                                f"reason={unhealthy_reason} "
                                f"connected_age={connected_age:.1f}s "
                                f"bad_count={bad_count} "
                                f"target_channel_id={target_channel_id}"
                            ),
                        )
                        await cleanup_stale_voice_client(vc, guild)
                        vc = await ensure_voice_connection(
                            guild,
                            target_channel_id,
                            move=True,
                            reason="watchdog_unhealthy",
                        )
                        if vc:
                            bot.reset_voice_retry(guild.id)
                        continue
                else:
                    bot.record_voice_health(guild.id, None)
                if target_channel_id:
                    await ensure_silence_keepalive(guild, vc, reason="watchdog_idle")
            except Exception as e:
                log_voice_debug(f"[VC] watchdog 재시작 실패 ({guild.name}): {e}")
            continue

        target_channel_id = bot.get_voice_target(guild.id)
        disconnected_age = bot.get_voice_disconnected_age(guild.id)
        if (
            not target_channel_id
            or disconnected_age is None
            or disconnected_age < VOICE_LIBRARY_RECOVERY_GRACE
            or not bot.can_retry_voice(guild.id)
        ):
            continue

        try:
            log_voice_snapshot(
                "watchdog 재접속 시도",
                guild,
                vc,
                extra=(
                    f"target_channel_id={target_channel_id} "
                    f"disconnected_age={disconnected_age:.1f}s"
                ),
            )
            vc = await ensure_voice_connection(
                guild,
                target_channel_id,
                move=True,
                reason="watchdog",
            )
            if vc:
                bot.reset_voice_retry(guild.id)
        except Exception as e:
            log_voice_debug(f"[VC] watchdog 재접속 실패 ({guild.name}): {e}")

@tasks.loop(seconds=5)
async def sound_watcher():
    """SOUND_DIR 변화 감지 → 자동 리스캔."""
    current_mtime = bot._get_sound_dir_mtime()
    if current_mtime != bot._sound_dir_mtime:
        bot._sound_dir_mtime = current_mtime
        old_count = len(bot.sound_files)
        bot.sound_files = scan_sound_files()
        new_count = len(bot.sound_files)
        if old_count != new_count:
            print(f"[SB] 사운드 자동 리스캔: {old_count} → {new_count}개")

@tasks.loop(seconds=LOOP_MONITOR_INTERVAL)
async def loop_watchdog():
    now = time_module.monotonic()
    expected = bot.loop_monitor_expected
    bot.loop_monitor_expected = now + LOOP_MONITOR_INTERVAL
    if expected is None:
        return

    lag = now - expected
    if lag < LOOP_MONITOR_WARN:
        return

    active_targets = sum(1 for guild in bot.guilds if bot.get_voice_target(guild.id))
    print(f"[LOOP] event loop lag 감지: {lag:.3f}s | active_voice_targets={active_targets}")
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
        if guild.voice_client is not None or target_channel_id:
            log_voice_snapshot(
                "event loop lag 시점",
                guild,
                guild.voice_client,
                extra=f"lag={lag:.3f}s target_channel_id={target_channel_id}",
            )

_sibo_generation = 0  # 시보 루프 세대 카운터

def restart_sibo():
    """시보 스케줄러를 취소하고 재시작. 세대 카운터로 중복 방지."""
    global _sibo_generation
    _sibo_generation += 1
    current_gen = _sibo_generation
    # 기존 task 취소
    if hasattr(bot, '_sibo_task') and bot._sibo_task and not bot._sibo_task.done():
        bot._sibo_task.cancel()
    bot._sibo_task = bot.loop.create_task(sibo_loop(current_gen))
    print(f"[SIBO] 스케줄러 재시작 (gen {current_gen})")

async def sibo_loop(my_gen=None):
    """정밀 시보 스케줄러. 세대 불일치 시 자동 종료."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        # 내가 최신 세대가 아니면 즉시 종료
        if my_gen is not None and my_gen != _sibo_generation:
            print(f"[SIBO] gen {my_gen} 종료 (최신: {_sibo_generation})")
            return
        all_sibo = bot.get_all_sibo()
        if not all_sibo:
            await asyncio.sleep(1)
            continue

        # 유효한 설정만 필터
        valid = {}
        for gid, cfg in all_sibo.items():
            sound_key = cfg.get("sound")
            if sound_key and sound_key in bot.sound_files:
                valid[gid] = cfg
        if not valid:
            await asyncio.sleep(1)
            continue

        # 가장 큰 offset 기준으로 sleep (가장 먼저 재생해야 하는 것)
        max_offset = max(c.get("offset", 0.0) for c in valid.values())
        total_advance = max_offset + SIBO_LATENCY_COMP

        now = datetime.now(KST)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        play_time = next_hour - timedelta(seconds=total_advance)
        wait_seconds = (play_time - now).total_seconds()

        if wait_seconds < 0:
            next_hour += timedelta(hours=1)
            play_time = next_hour - timedelta(seconds=total_advance)
            wait_seconds = (play_time - now).total_seconds()

        target_hour = next_hour.strftime('%H:00:00')
        print(f"[SIBO] 목표 정각: {target_hour} | 최대 offset: {max_offset}s | {wait_seconds:.1f}초 후")

        if LAVALINK_ENABLED:
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                print(f"[SIBO] gen {my_gen} 취소됨")
                return

            if my_gen is not None and my_gen != _sibo_generation:
                print(f"[SIBO] gen {my_gen} 종료 (최신: {_sibo_generation})")
                return

            valid = {}
            for gid, cfg in bot.get_all_sibo().items():
                sound_key = cfg.get("sound")
                if sound_key and sound_key in bot.sound_files:
                    valid[gid] = cfg

            last_wait = 0.0
            for gid, cfg in sorted(valid.items(), key=lambda item: item[1].get("offset", 0.0), reverse=True):
                sound_key = cfg.get("sound")
                offset = cfg.get("offset", 0.0)
                filepath = bot.sound_files.get(sound_key)
                if not filepath:
                    continue

                wait_delta = max(0.0, last_wait - offset)
                if wait_delta > 0:
                    await asyncio.sleep(wait_delta)
                last_wait = offset

                guild = bot.get_guild(int(gid))
                if not guild:
                    continue

                vc = guild.voice_client
                if not vc or not voice_is_connected(vc):
                    vc_channel_id = cfg.get("vc_channel")
                    if not vc_channel_id:
                        continue
                    try:
                        vc = await ensure_voice_connection(
                            guild,
                            vc_channel_id,
                            move=True,
                            reason="sibo_lavalink_reconnect",
                        )
                    except Exception as e:
                        print(f"[SIBO] Guild {gid} Lavalink VC 재접속 실패: {e}")
                        continue

                if not vc:
                    continue

                try:
                    if voice_is_playing(vc):
                        await voice_stop(vc)
                    await play_lavalink_local_sound(
                        vc,
                        filepath,
                        bot.sound_volume,
                        replace=True,
                        lead_in_ms=LAVALINK_SIBO_PAD_MS,
                    )
                    play_now = datetime.now(KST)
                    print(f"[SIBO] {play_now.strftime('%H:%M:%S.%f')[:-4]} Guild {gid} 시보 재생(Lavalink): {sound_key}")
                except Exception as e:
                    print(f"[SIBO] Guild {gid} Lavalink 재생 실패: {e}")

            await asyncio.sleep(1)
            continue

        # 1) play_time 보다 PRELOAD_LEAD 일찍 깨서 ffmpeg 미리 준비
        pre_warm_wait = max(0.0, wait_seconds - SIBO_PRELOAD_LEAD)
        try:
            await asyncio.sleep(pre_warm_wait)
        except asyncio.CancelledError:
            print(f"[SIBO] gen {my_gen} 취소됨")
            return

        # 세대 체크 (pre-warm 진입 전)
        if my_gen is not None and my_gen != _sibo_generation:
            print(f"[SIBO] gen {my_gen} 종료 (최신: {_sibo_generation})")
            return

        # 변경됐을 수 있으니 최신 설정 다시 읽기
        valid = {}
        for gid, cfg in bot.get_all_sibo().items():
            sound_key = cfg.get("sound")
            if sound_key and sound_key in bot.sound_files:
                valid[gid] = cfg

        # 2) 사전 생성 패스: 전 서버 FFmpegPCMAudio 동시 spawn (파이프 버퍼 채우기 시작)
        guild_audio = {}
        for gid, cfg in valid.items():
            sound_key = cfg.get("sound")
            filepath = bot.sound_files.get(sound_key)
            if not filepath:
                continue
            try:
                guild_audio[gid] = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(filepath, options="-af loudnorm=I=-16:TP=-1.5:LRA=11"),
                    volume=bot.sound_volume
                )
            except Exception as e:
                print(f"[SIBO] Guild {gid} 사전 생성 실패: {e}")

        # 3) 남은 PRELOAD_LEAD 만큼 대기 (ffmpeg가 파이프 버퍼 채울 시간) → play_time 도달
        if pre_warm_wait > 0:
            try:
                await asyncio.sleep(SIBO_PRELOAD_LEAD)
            except asyncio.CancelledError:
                for a in guild_audio.values():
                    try: a.cleanup()
                    except Exception: pass
                print(f"[SIBO] gen {my_gen} 취소됨")
                return

        # 세대 재체크 (취소/전환됐으면 pre-created subprocess 정리)
        if my_gen is not None and my_gen != _sibo_generation:
            for a in guild_audio.values():
                try: a.cleanup()
                except Exception: pass
            print(f"[SIBO] gen {my_gen} 종료 (최신: {_sibo_generation})")
            return

        # 4) 타이밍 + 재생 패스: offset별 순차 재생 (이미 준비된 audio 사용)
        max_offset = max((c.get("offset", 0.0) for c in valid.values()), default=0)
        for gid, cfg in valid.items():
            sound_key = cfg.get("sound")
            offset = cfg.get("offset", 0.0)

            # 이 서버의 재생까지 남은 추가 대기
            extra_wait = max_offset - offset
            if extra_wait > 0:
                await asyncio.sleep(extra_wait)

            audio = guild_audio.get(gid)
            if not audio:
                continue

            guild = bot.get_guild(int(gid))
            if not guild:
                try: audio.cleanup()
                except Exception: pass
                continue
            vc = guild.voice_client
            if not vc or not voice_is_connected(vc):
                # VC 끊겼으면 저장된 채널로 자동 재접속
                vc_channel_id = cfg.get("vc_channel")
                if vc_channel_id:
                    channel = guild.get_channel(vc_channel_id)
                    if channel:
                        try:
                            vc = await ensure_voice_connection(
                                guild,
                                vc_channel_id,
                                move=True,
                                reason="sibo_reconnect",
                            )
                            if not vc:
                                try: audio.cleanup()
                                except Exception: pass
                                continue
                            print(f"[SIBO] Guild {gid} VC 자동 재접속: {channel.name}")
                        except Exception as e:
                            print(f"[SIBO] Guild {gid} VC 재접속 실패: {e}")
                            try: audio.cleanup()
                            except Exception: pass
                            continue
                    else:
                        try: audio.cleanup()
                        except Exception: pass
                        continue
                else:
                    try: audio.cleanup()
                    except Exception: pass
                    continue

            if voice_is_playing(vc):
                await voice_stop(vc)
            def after_sibo_play(error, guild_id=int(gid), sound_name=sound_key):
                if error:
                    print(f"[SIBO] Guild {guild_id} 재생 종료 오류: {error}")
                schedule_silence_keepalive_resume(
                    guild_id,
                    reason=f"sibo:{sound_name}",
                )

            vc.play(audio, after=after_sibo_play)
            play_now = datetime.now(KST)
            print(f"[SIBO] {play_now.strftime('%H:%M:%S.%f')[:-4]} Guild {gid} 시보 재생: {sound_key}")

        await asyncio.sleep(1)

PANEL_EMBED_TITLE_PREFIXES = (
    "🎛 Gemini 패널",
    "🔊 사운드보드",
    "⏰ 시보 설정",
)


def is_panel_message(message):
    if message.author != bot.user:
        return False
    return any(
        (embed.title or "").startswith(prefix)
        for embed in message.embeds
        for prefix in PANEL_EMBED_TITLE_PREFIXES
    )


async def ensure_control_panel(guild, channel, *, save_target=False):
    """채널 전체를 지우지 않고 통합 패널 메시지 하나만 준비한다."""
    if save_target:
        config = bot.get_config(guild.id)
        config["sb_channel_id"] = channel.id
        bot.save_config()

    panel_view = ControlPanelView(guild.id)
    bot.active_views.append(panel_view)

    panel_messages = []
    try:
        async for msg in channel.history(limit=50):
            if is_panel_message(msg):
                panel_messages.append(msg)
    except Exception as e:
        print(f"[PANEL] {guild.name}: 기존 패널 확인 실패: {e}")

    if panel_messages:
        target = panel_messages[0]
        await target.edit(embed=panel_view._make_embed(), view=panel_view)
        for stale in panel_messages[1:4]:
            try:
                await stale.delete()
            except Exception as e:
                print(f"[PANEL] {guild.name}: 중복 패널 삭제 실패: {e}")
        print(f"[PANEL] {guild.name}: #{channel.name} 통합 패널 갱신 완료")
        return "updated"

    await channel.send(embed=panel_view._make_embed(), view=panel_view)
    print(f"[PANEL] {guild.name}: #{channel.name} 통합 패널 생성 완료")
    return "created"


async def setup_panel_channels():
    """봇 시작 시 저장된 채널의 통합 패널을 갱신한다."""
    for guild in bot.guilds:
        config = bot.get_config(guild.id)
        panel_channel_id = config.get("sb_channel_id")
        if not panel_channel_id:
            continue
        channel = guild.get_channel(panel_channel_id)
        if not channel:
            print(f"[PANEL] {guild.name}: 패널 채널 없음 (ID: {panel_channel_id})")
            continue
        try:
            await ensure_control_panel(guild, channel)
        except Exception as e:
            print(f"[PANEL] {guild.name}: 임베드 생성 실패: {e}")

@bot.event
async def on_ready():
    print(f"[OK] {bot.user} 로그인 완료!")
    try:
        synced = await bot.tree.sync()
        print(f"[OK] {len(synced)}개의 슬래시 커맨드 동기화 완료")
    except Exception as e:
        print(f"[ERROR] 슬래시 커맨드 동기화 실패: {e}")
    if LAVALINK_ENABLED:
        try:
            node = await bot.ensure_lavalink_node()
            session_id = getattr(node, "session_id", None)
            print(f"[OK] Lavalink 연결 완료: {LAVALINK_HOST}:{LAVALINK_PORT} label={LAVALINK_LABEL} session_id={session_id}")
        except Exception as e:
            print(f"[ERROR] Lavalink 연결 실패: {e}")
    if not voice_watchdog.is_running():
        voice_watchdog.start()
    if not sound_watcher.is_running():
        sound_watcher.start()
    if not loop_watchdog.is_running():
        bot.loop_monitor_expected = None
        loop_watchdog.start()
    if not hasattr(bot, '_sibo_task') or bot._sibo_task is None or bot._sibo_task.done():
        global _sibo_generation
        _sibo_generation += 1
        bot._sibo_task = bot.loop.create_task(sibo_loop(_sibo_generation))
    print(f"[OK] 사운드 감시 + 시보 스케줄러 시작")
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
        if guild.voice_client and guild.voice_client.is_connected():
            bot.mark_voice_connected(guild.id)
            await ensure_silence_keepalive(guild, guild.voice_client, reason="on_ready")
        if target_channel_id:
            log_voice_snapshot(
                "on_ready keepalive 상태",
                guild,
                guild.voice_client,
                extra=f"target_channel_id={target_channel_id}",
            )
    await setup_panel_channels()

@bot.event
async def on_disconnect():
    active = sum(1 for guild in bot.guilds if guild.voice_client is not None)
    log_voice_debug(f"[GATEWAY] on_disconnect | active_voice_clients={active}")
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
        if guild.voice_client is not None or target_channel_id:
            log_voice_snapshot(
                "gateway disconnect 시점",
                guild,
                guild.voice_client,
                extra=f"target_channel_id={target_channel_id}",
            )

@bot.event
async def on_resumed():
    active = sum(1 for guild in bot.guilds if guild.voice_client is not None)
    log_voice_debug(f"[GATEWAY] on_resumed | active_voice_clients={active}")
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
        if guild.voice_client is not None or target_channel_id:
            log_voice_snapshot(
                "gateway resumed 시점",
                guild,
                guild.voice_client,
                extra=f"target_channel_id={target_channel_id}",
            )

@bot.event
async def on_node_ready(node):
    if getattr(node, "session_id", None):
        bot.set_lavalink_session_id(node.session_id)
    print(
        f"[LAVALINK] node ready: label={getattr(node, 'label', '-')}"
        f" session_id={getattr(node, 'session_id', None)}"
    )

@bot.event
async def on_node_unavailable(node):
    print(f"[LAVALINK] node unavailable: label={getattr(node, 'label', '-')}")

@bot.event
async def on_websocket_closed(event):
    player = getattr(event, "player", None)
    guild_id = getattr(getattr(player, "guild", None), "id", None)
    print(
        f"[LAVALINK] websocket closed: guild={guild_id} code={getattr(event, 'code', None)} "
        f"reason={getattr(event, 'reason', None)} by_discord={getattr(event, 'by_discord', None)}"
    )

@bot.event
async def on_track_exception(event):
    player = getattr(event, "player", None)
    guild_id = getattr(getattr(player, "guild", None), "id", None)
    print(f"[LAVALINK] track exception: guild={guild_id} error={getattr(event, 'exception', None)}")

@bot.event
async def on_voice_state_update(member, before, after):
    """봇의 VC 상태 변화 추적: 목표 채널 저장 + 끊긴 경우 빠른 재접속."""
    if member != bot.user:
        return

    log_voice_snapshot(
        "bot voice_state_update",
        member.guild,
        member.guild.voice_client,
        extra=f"before=({describe_voice_state(before)}) after=({describe_voice_state(after)}) target_channel_id={bot.get_voice_target(member.guild.id)}",
    )

    if after.channel is not None:
        bot.set_voice_target(member.guild.id, after.channel.id)
        bot.reset_voice_retry(member.guild.id)
        bot.mark_voice_connected(member.guild.id)
        await ensure_silence_keepalive(
            member.guild,
            member.guild.voice_client,
            reason="voice_state_update_connected",
        )
        return

    target_channel_id = bot.get_voice_target(member.guild.id)
    if not target_channel_id:
        return

    bot.mark_voice_disconnected(member.guild.id)
    log_voice_debug(
        f"[VC] bot 음성 채널 이탈 감지 ({member.guild.name}): "
        f"target_channel_id={target_channel_id}, "
        f"discord.py 내부 복구 우선, {VOICE_LIBRARY_RECOVERY_GRACE:.0f}초 후 watchdog 개입"
    )
    asyncio.create_task(attempt_voice_state_rejoin(member.guild.id, target_channel_id))

@bot.event
async def on_message(message):
    if message.author.bot: return
    if not message.guild: return

    # 사운드보드 업로드 채널 체크
    config = bot.get_config(message.guild.id)
    sb_channel_id = config.get("sb_channel_id")
    if sb_channel_id and message.channel.id == sb_channel_id and message.attachments:
        dl_dir = os.path.join(SOUND_DIR, "discord")
        os.makedirs(dl_dir, exist_ok=True)
        # 메시지 텍스트가 있으면 파일명으로 사용 (한글 파일명 깨짐 방지)
        custom_names = [n.strip() for n in message.content.split(",") if n.strip()] if message.content.strip() else []
        added = []
        for i, att in enumerate(message.attachments):
            ext = os.path.splitext(att.filename)[1].lower()
            if ext in SOUND_EXTENSIONS:
                if i < len(custom_names):
                    filename = custom_names[i] + ext
                else:
                    filename = att.filename
                filename = unicodedata.normalize("NFC", filename)
                save_path = os.path.join(dl_dir, filename)
                await att.save(save_path)
                name = "discord/" + os.path.splitext(filename)[0].lower()
                added.append(name)
        if added:
            bot.sound_files = scan_sound_files()
            tip = "\n💡 파일명 지정: 메시지에 이름을 적으면 그 이름으로 저장됩니다." if not custom_names else ""
            reply = await message.reply(f"사운드 추가됨: **{', '.join(added)}**\n현재 {len(bot.sound_files)}개{tip}", mention_author=False)
            # 5초 후 원본 메시지 + 확인 메시지 삭제
            await asyncio.sleep(5)
            try:
                await message.delete()
            except Exception as e:
                print(f"[SB] 업로드 메시지 삭제 실패: {e}")
            try:
                await reply.delete()
            except Exception as e:
                print(f"[SB] 응답 메시지 삭제 실패: {e}")
        return

    if message.channel.id != config.get("channel_id"): return

    lock = bot.get_channel_lock(message.channel.id)
    async with lock:
        async with message.channel.typing():
            try:
                server_text_model = config.get("text_model", TEXT_MODEL)
                server_image_model = config.get("image_model", IMAGE_MODEL)
                history_limit = get_history_limit(config)
                image_history_limit = get_image_history_limit(config)
                history_scan_limit = max(history_limit, image_history_limit)
                reset_ts = datetime.fromisoformat(config["reset_time"]) if config.get("reset_time") else None
                reply_target_id = message.reference.message_id if message.reference and message.reference.message_id else None

                history_list = []
                history_texts = []
                history_meta = []
                history_position = 0
                history_inline_images = 0
                history_ref_images = []

                async for msg in message.channel.history(limit=history_scan_limit + 10):
                    if msg.id == message.id: continue
                    if msg.id > message.id and msg.author != bot.user: continue
                    if reset_ts and msg.created_at < reset_ts: break
                    if msg.author == bot.user and getattr(msg, "interaction_metadata", None) is not None:
                        continue

                    within_text_history = history_position < history_limit
                    within_image_history = history_position < image_history_limit

                    cached_doc_blocks = []
                    if within_text_history and msg.author != bot.user:
                        replay_full_docs = (reply_target_id == msg.id) or (history_position < DOC_CACHE_AUTO_REPLAY_WINDOW)
                        cached_doc_blocks = bot.doc_cache.render_blocks(msg.id, full_content=replay_full_docs)

                    image_parts = []
                    if msg.author == bot.user:
                        content = msg.content if within_text_history else ""
                        if within_text_history and ('"action":' in content or '"prompt":' in content):
                            content = "[System Log: Action Performed]"
                        elif within_text_history and "Error" in content:
                            content = "[System Error Log]"
                        if msg.attachments:
                            for att in msg.attachments:
                                if is_image_attachment(att):
                                    # 봇이 과거 생성/전송한 이미지: 마커 대신 실제 이미지 재첨부 (이미지 윈도우 내).
                                    # '(Bot sent an image: ...)' 마커를 모델 role 히스토리에 남기면 모델이 그 텍스트를
                                    # 흉내내서 generate_image 툴 호출 대신 마커만 출력함. 마커는 절대 남기지 않음.
                                    if within_image_history:
                                        d, m = await download_media(att.url)
                                        if d:
                                            raw = d.getvalue()
                                            m = infer_attachment_mime(att, m)
                                            image_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=raw)))
                                            history_inline_images += 1
                                            try:
                                                history_ref_images.append(Image.open(io.BytesIO(raw)).copy())
                                            except Exception:
                                                pass
                                    # 윈도우 밖이면 마커 없이 생략
                                elif within_text_history and att.content_type and att.content_type.startswith('video'):
                                    content += f"\n(Bot generated a video here: {att.filename})"
                    else:
                        content = msg.content if (within_text_history or within_image_history) else ""
                        if msg.attachments:
                            for att in msg.attachments:
                                if is_image_attachment(att):
                                    # 이미지 윈도우 내: 실제 이미지 재첨부(마커 없음). 윈도우 밖: 마커로만 컨텍스트 표시.
                                    reattached = False
                                    if within_image_history:
                                        d, m = await download_media(att.url)
                                        if d:
                                            raw = d.getvalue()
                                            m = infer_attachment_mime(att, m)
                                            image_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=raw)))
                                            history_inline_images += 1
                                            try:
                                                history_ref_images.append(Image.open(io.BytesIO(raw)).copy())
                                            except Exception:
                                                pass
                                            reattached = True
                                        else:
                                            content += f"\n(User attached an image: {att.filename}) (image download failed)"
                                    if not reattached and within_text_history and not within_image_history:
                                        content += f"\n(User attached an image: {att.filename})"
                                elif within_text_history and is_audio_or_video_attachment(att):
                                    media_type = attachment_media_type(att)
                                    content += f"\n(User attached a {media_type} file: {att.filename})"
                                elif within_text_history:
                                    file_text, file_kind = await extract_attachment_text(att)
                                    if file_text and not bot.doc_cache.has_source(msg.id, "text_file", att.filename):
                                        bot.doc_cache.store_document("text_file", att.filename, file_text, msg.id, msg.channel.id)
                                        content += f"\n\n[Attached File Content: {att.filename}]\n```\n{file_text}\n```"
                                    elif file_kind:
                                        content += f"\n(User attached a {file_kind} file: {att.filename})"

                    if cached_doc_blocks:
                        content += "\n\n" + "\n\n".join(cached_doc_blocks)

                    role = "model" if msg.author == bot.user else "user"
                    if role == "user":
                        content = prepend_user_identity(content, message.guild, msg.author, kind="history")
                    parts = []
                    if content.strip():
                        parts.append(types.Part(text=content))
                    parts.extend(image_parts)
                    if not parts:
                        history_position += 1
                        if history_position >= history_scan_limit:
                            break
                        continue
                    history_list.insert(0, types.Content(role=role, parts=parts))
                    history_texts.insert(0, content)
                    # 후처리(봇 응답 대상 마커)용 메타 — history_list와 1:1
                    user_info = (
                        None if msg.author == bot.user
                        else (get_server_display_name(message.guild, msg.author), str(msg.author.id))
                    )
                    history_meta.insert(0, {"is_bot": msg.author == bot.user, "user_info": user_info})
                    history_position += 1
                    if history_position >= history_scan_limit:
                        break

                # 봇 응답에 [Bot replied to ...] prefix 추가 (직전 user 메시지 기준)
                last_user_info = None
                for idx, meta in enumerate(history_meta):
                    if meta["is_bot"]:
                        if last_user_info:
                            cur = history_list[idx]
                            if cur.parts and cur.parts[0].text:
                                new_text = prepend_bot_target(cur.parts[0].text, *last_user_info)
                                history_list[idx] = types.Content(role=cur.role, parts=[types.Part(text=new_text)])
                                history_texts[idx] = new_text
                    else:
                        if meta["user_info"]:
                            last_user_info = meta["user_info"]

                print(
                    f"[HISTORY] count={len(history_list)} text_window={history_limit} "
                    f"image_window={image_history_limit} inline_images={history_inline_images}"
                )

                current_display_name = get_server_display_name(message.guild, message.author)
                # 현재 메시지 본문에 [CURRENT MESSAGE from ...] prefix 적용
                current_text_with_marker = prepend_user_identity(message.content, message.guild, message.author, kind="current")
                current_parts = [types.Part(text=current_text_with_marker)] if current_text_with_marker else []
                current_ref_images = []
                has_current_images = False

                urls = re.findall(r'(https?://\S+)', message.content)
                for url in urls:
                    web_content = await scrap_webpage(url)
                    if web_content:
                        doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
                        print(f"[CACHE] webpage 저장: {doc_id} ({url})")
                        current_parts.append(types.Part(text=f"\n\n[Webpage Content: {url}]\n```\n{web_content}\n```"))
                        print(f"[WEB] 웹사이트 내용 읽음: {url}")

                if message.attachments:
                    for att in message.attachments:
                        if is_image_attachment(att):
                            d, m = await download_media(att.url)
                            if d:
                                raw = d.getvalue()
                                m = infer_attachment_mime(att, m)
                                current_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=raw)))
                                has_current_images = True
                                try:
                                    current_ref_images.append(Image.open(io.BytesIO(raw)).copy())
                                except Exception:
                                    pass
                                bot.recent_image_store.register_bytes(
                                    message.guild.id,
                                    channel_id=message.channel.id,
                                    message_id=message.id,
                                    author_id=message.author.id,
                                    display_name=current_display_name,
                                    filename=att.filename,
                                    image_bytes=raw,
                                    source_kind="user_upload",
                                )
                            else:
                                current_parts.append(types.Part(text=f"\n(User attached an image: {att.filename}, but it could not be downloaded.)"))
                        elif is_audio_or_video_attachment(att):
                            d, m = await download_media(att.url)
                            if d:
                                file_bytes = d.getvalue()
                                media_type = attachment_media_type(att)
                                m = infer_attachment_mime(att, m)
                                if len(file_bytes) < 15 * 1024 * 1024:
                                    current_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=file_bytes)))
                                else:
                                    file_ref = await upload_file_api(file_bytes, m)
                                    if file_ref:
                                        current_parts.append(types.Part(file_data=types.FileData(file_uri=file_ref.uri, mime_type=m)))
                                print(f"[{media_type.upper()}] {media_type} 첨부: {att.filename} ({len(file_bytes)} bytes)")
                        else:
                            file_content, file_kind = await extract_attachment_text(att)
                            if file_content:
                                doc_id = bot.doc_cache.store_document("text_file", att.filename, file_content, message.id, message.channel.id)
                                print(f"[CACHE] {file_kind or 'text'} file 저장: {doc_id} ({att.filename})")
                                current_parts.append(types.Part(text=f"\n\n[Attached File Content: {att.filename}]\n```\n{file_content}\n```"))

                route = {
                    "action": "respond_text",
                    "refined_prompt": "",
                    "use_recent_images": False,
                    "reason": "action_router_skipped",
                }
                action_router_enabled = is_enabled(config.get("action_router", DEFAULT_ACTION_ROUTER_ENABLED))
                recent_images_available = bool(bot.recent_image_store.list_recent(message.guild.id, channel_id=message.channel.id))
                recent_context = build_recent_conversation_excerpt(history_texts)
                direct_recent_image_inspection = (
                    recent_images_available
                    and not has_current_images
                    and has_recent_image_inspection_signal(message.content)
                )
                if direct_recent_image_inspection:
                    route = {
                        "action": "inspect_recent_images",
                        "refined_prompt": "",
                        "use_recent_images": True,
                        "reason": "direct_recent_image_inspection_signal",
                    }
                elif action_router_enabled and should_call_action_router(
                    message.content,
                    has_current_images=has_current_images,
                    has_recent_images=recent_images_available,
                    allow_image_generation=bool(server_image_model),
                    allow_recent_images=True,
                ):
                    route = await route_message_action(
                        message.content,
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        has_current_images=has_current_images,
                        text_model=server_text_model,
                        recent_context=recent_context,
                    )

                if route.get("action") == "generate_image":
                    display_prompt = route.get("refined_prompt") or message.content
                    generation_prompt = build_contextual_image_prompt(route.get("refined_prompt"), message.content, history_texts)
                    ref_images = []
                    if current_ref_images:
                        ref_images = current_ref_images
                    elif route.get("use_recent_images"):
                        ref_images = list(history_ref_images)

                    print(f"[IMG] 라우팅 생성 요청: '{display_prompt}' (현재첨부={len(current_ref_images)} 최근참조={len(ref_images)} ctx={len(recent_context)})")
                    status_msg = await message.channel.send(f"**Prompt:** `{compact_prompt_for_status(display_prompt)}`")
                    req_content = ref_images + [generation_prompt]
                    try:
                        img_data = await call_nano_banana(req_content, image_model=server_image_model)
                        bot.recent_image_store.register_bytes(
                            message.guild.id,
                            channel_id=message.channel.id,
                            message_id=message.id,
                            author_id=bot.user.id,
                            display_name=get_bot_display_name(),
                            filename="generated.png",
                            image_bytes=img_data,
                            source_kind="bot_generated",
                        )
                        with io.BytesIO(img_data) as binary:
                            binary.seek(0)
                            fn = "edited.png" if ref_images else "generated.png"
                            await message.channel.send(file=discord.File(fp=binary, filename=fn))
                        await status_msg.edit(content=f"[OK] **Generated Prompt:** `{compact_prompt_for_status(display_prompt)}`")
                        print(f"[BOT]({server_image_model}): [이미지 생성 완료]")
                    except Exception as e:
                        err_msg = f"[ERROR] Image Generation Failed: {str(e)}"
                        print(err_msg)
                        await status_msg.edit(content=err_msg)
                    return

                if route.get("action") == "inspect_recent_images" and not has_current_images and route.get("use_recent_images"):
                    print(
                        f"[IMG] 과거 이미지 요청: history_inline_images={history_inline_images} "
                        f"image_history={image_history_limit} reason={route.get('reason')}"
                    )

                # 현재 메시지의 [CURRENT MESSAGE from ...] prefix는 이미 current_parts[0]에 포함됨
                full_contents = history_list + [types.Content(role="user", parts=current_parts)]

                response, token_usage = await call_gemini(
                    full_contents,
                    text_model=server_text_model,
                    thinking_level=config.get("thinking_level"),
                )

                if isinstance(response, str):
                    response = clean_assistant_response_text(response)
                    await send_long_message(message.channel, response)
                    return

                status = get_interaction_status(response)
                if status not in {"completed", "incomplete"}:
                    error = get_interaction_output_value(response, "error")
                    detail = f": {error}" if error else ""
                    await message.channel.send(f"[ERROR] 응답 생성 중단 (status={status}){detail}")
                    return
                if status == "incomplete":
                    print("[WARN] Gemini interaction이 incomplete 상태로 종료되어 부분 응답을 전송합니다.")

                log_time = datetime.now().strftime('%H:%M:%S')
                print(f"[{log_time}] [{message.guild.name}] {current_display_name}: {message.content}")

                raw_text_reply = extract_interaction_text(response)
                text_reply = clean_assistant_response_text(raw_text_reply)
                if not text_reply:
                    print(f"[WARN] 빈 응답 또는 식별 마커만 반환됨 (status: {status})")
                    await message.channel.send("[WARN] 모델이 내용 없는 응답을 반환했습니다. 다시 시도해주세요.")
                    return

                print(f"[BOT]({server_text_model}): {text_reply} (Tokens: {token_usage}, Status: {status})")
                await send_long_message(message.channel, text_reply)

                # video_calls 처리 — 비용 절감으로 임시 비활성화

                print("-------")

            except Exception as e:
                print(f"[CRITICAL] Error: {e}")
                await message.channel.send(f"[ERROR] 오류 발생: {e}")

# ================= 5. 서버 설정 명령어 =================

@bot.tree.command(name="set", description="현재 채널을 봇 활성화 채널로 설정")
async def set_ch(interaction: discord.Interaction):
    if not interaction.guild:
        await temp_reply(interaction, "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    config["channel_id"] = interaction.channel_id
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 봇 활성화 채널을 {interaction.channel.mention} 으로 설정했습니다."
    )


@bot.tree.command(name="history", description="Gemini 대화/이미지 히스토리 참조 개수 설정")
@app_commands.describe(
    count=f"텍스트로 참고할 최근 메시지 개수 ({MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)",
    image_count=f"이미지를 실제 재첨부할 최근 메시지 개수 ({MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=끄기)",
)
async def set_history(
    interaction: discord.Interaction,
    count: int | None = None,
    image_count: int | None = None,
):
    if not interaction.guild:
        await temp_reply(interaction, "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    if count is None and image_count is None:
        await interaction.response.send_message(
            f"현재 Gemini 원문 히스토리 참조: **{get_history_limit(config)}개** "
            f"(범위 {MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)\n"
            f"현재 Gemini 이미지 히스토리 재첨부: **{get_image_history_limit(config)}개** "
            f"(범위 {MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=끄기)"
        )
        return
    changed = []
    if count is not None:
        limit = clamp_history_limit(count)
        config["history_limit"] = limit
        suffix = "" if limit == count else f" (요청값 {count}에서 허용 범위로 보정)"
        changed.append(f"원문 히스토리 **{limit}개**{suffix}")
    if image_count is not None:
        image_limit = clamp_image_history_limit(image_count)
        config["image_history_limit"] = image_limit
        suffix = "" if image_limit == image_count else f" (요청값 {image_count}에서 허용 범위로 보정)"
        changed.append(f"이미지 히스토리 **{image_limit}개**{suffix}")
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 Gemini " + ", ".join(changed) + "로 설정했습니다."
    )

@bot.tree.command(name="reset", description="히스토리 초기화")
async def reset_history(interaction: discord.Interaction):
    if not interaction.guild:
        await temp_reply(interaction, "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    bot.save_config()
    await interaction.response.send_message("[OK] 히스토리 초기화")

# ================= 6. User Install 슬래시 커맨드 =================

@bot.tree.command(name="chat", description="AI와 대화합니다 (이미지 분석 가능)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    message="질문 또는 메시지",
    image="분석할 이미지 파일 (선택)",
    image_url="분석할 이미지 URL (선택, Discord에 업로드 후 링크 복사)"
)
async def chat_command(interaction: discord.Interaction, message: str, image: discord.Attachment = None, image_url: str = None):
    await interaction.response.defer()

    log_time = datetime.now().strftime('%H:%M:%S')
    location = f"[{interaction.guild.name}]" if interaction.guild else "[DM]"
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    print(f"[{log_time}] {location} [/chat] {actor_name}: {message}")

    try:
        parts = [types.Part(text=message)]

        img_data = None
        mime_type = None
        image_source = None

        if image and is_image_attachment(image):
            img_data, mime_type = await download_media(image.url)
            if img_data:
                mime_type = infer_attachment_mime(image, mime_type)
                parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=img_data.getvalue())))
                image_source = image.url
                print(f"  [IMG] 이미지 첨부: {image.url}")
        elif image_url:
            img_data, mime_type = await download_media(image_url)
            if img_data:
                parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=img_data.getvalue())))
                image_source = image_url
                print(f"  [IMG] 이미지 URL: {image_url}")

        # 사용자 입력 표시
        user_display = f"**{actor_name}**: {message}"
        if image_source:
            embed = discord.Embed(description=user_display)
            embed.set_image(url=image_source)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(user_display)

        contents = [types.Content(role="user", parts=parts)]

        response = await create_model_interaction(
            model=TEXT_MODEL,
            input_data=gemini_contents_to_interaction_steps(contents),
            system_instruction=CHAT_SYSTEM_PROMPT,
            tools=[{"type": "google_search"}],
        )
        token_usage = get_interaction_usage(response)

        status = get_interaction_status(response)
        if status not in {"completed", "incomplete"}:
            await interaction.followup.send(f"[ERROR] 응답 생성 중단 (status={status})")
            return
        if status == "incomplete":
            print("  [WARN] Gemini interaction이 incomplete 상태로 종료되어 부분 응답을 전송합니다.")

        text_reply = extract_interaction_text(response)

        if text_reply.strip():
            if len(text_reply) > 2000:
                chunks = [text_reply[i:i+1900] for i in range(0, len(text_reply), 1900)]
                for chunk in chunks:
                    await interaction.followup.send(chunk)
            else:
                await interaction.followup.send(text_reply)

            print(f"  [BOT]({TEXT_MODEL}): {text_reply}")
            print(f"  [TOKEN]: {token_usage}")
        else:
            await interaction.followup.send("[WARN] 응답을 생성하지 못했습니다.")

        print("-------")

    except Exception as e:
        print(f"  [ERROR]: {e}")
        print("-------")
        await interaction.followup.send(f"[ERROR] 오류 발생: {e}")


@bot.tree.command(name="image", description="이미지를 생성하거나 편집합니다")
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    prompt="생성할 이미지에 대한 설명",
    reference="편집할 원본 이미지 파일 (선택)",
    reference_url="편집할 이미지 URL (선택, Discord에 업로드 후 링크 복사)"
)
async def image_command(interaction: discord.Interaction, prompt: str, reference: discord.Attachment = None, reference_url: str = None):
    await interaction.response.defer()

    log_time = datetime.now().strftime('%H:%M:%S')
    location = f"[{interaction.guild.name}]" if interaction.guild else "[DM]"
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    print(f"[{log_time}] {location} [/image] {actor_name}: {prompt}")

    try:
        ref_images = []
        has_reference = False
        reference_source = None

        if reference and is_image_attachment(reference):
            img_data, _ = await download_media(reference.url)
            if img_data:
                ref_images.append(Image.open(img_data))
                has_reference = True
                reference_source = reference.url
                print(f"  [IMG] 참조 이미지: {reference.url}")
        elif reference_url:
            img_data, _ = await download_media(reference_url)
            if img_data:
                ref_images.append(Image.open(img_data))
                has_reference = True
                reference_source = reference_url
                print(f"  [IMG] 참조 이미지 URL: {reference_url}")

        user_display = f"**{actor_name}**: {prompt}"
        if reference_source:
            embed = discord.Embed(description=user_display)
            embed.set_image(url=reference_source)
            embed.set_footer(text="원본 이미지")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(user_display)

        user_message = f"User request: {prompt}"
        if has_reference:
            user_message += "\n(A reference image is provided for editing)"
        user_message += "\n\nReturn the refined image prompt and an optional brief user-facing message."

        response = await create_model_interaction(
            model=TEXT_MODEL,
            input_data=user_message,
            system_instruction=IMAGE_SYSTEM_PROMPT,
            response_schema=IMAGE_PROMPT_SCHEMA,
        )

        status = get_interaction_status(response)
        if status not in {"completed", "incomplete"}:
            await interaction.followup.send(f"[ERROR] 이미지 프롬프트 생성 중단 (status={status})")
            return
        if status == "incomplete":
            print("  [WARN] 이미지 프롬프트 interaction이 incomplete 상태로 종료되었습니다.")

        prompt_payload = coerce_json_object(extract_interaction_text(response)) or {}
        generated_prompt = (prompt_payload.get("prompt") or prompt).strip()
        text_reply = (prompt_payload.get("message") or "").strip()

        print(f"  [PROMPT] 정제된 프롬프트: {generated_prompt}")

        if text_reply.strip():
            clean_reply = re.sub(r'\{.*"prompt".*\}', '', text_reply, flags=re.DOTALL).strip()
            clean_reply = clean_assistant_response_text(clean_reply)
            if clean_reply:
                if len(clean_reply) > 1900:
                    clean_reply = clean_reply[:1900] + "..."
                await interaction.followup.send(clean_reply)
                print(f"  [BOT]({TEXT_MODEL}): {clean_reply}")

        status_msg = await interaction.followup.send(f"**Generating:** `{generated_prompt}`")

        req_content = ref_images + [generated_prompt] if ref_images else [generated_prompt]
        img_data = await call_nano_banana(req_content)
        if interaction.guild_id is not None:
            bot.recent_image_store.register_bytes(
                interaction.guild_id,
                channel_id=interaction.channel_id,
                message_id=interaction.id,
                author_id=bot.user.id,
                display_name=get_bot_display_name(),
                filename="generated.png",
                image_bytes=img_data,
                source_kind="bot_generated",
            )
        with io.BytesIO(img_data) as binary:
            binary.seek(0)
            fn = "edited.png" if ref_images else "generated.png"
            await interaction.followup.send(file=discord.File(fp=binary, filename=fn))

        await status_msg.edit(content=f"[OK] **Generated:** `{generated_prompt}`")
        print(f"  [BOT]({IMAGE_MODEL}): [이미지 생성 완료]")
        print("-------")

    except Exception as e:
        print(f"  [ERROR]: {e}")
        print("-------")
        await interaction.followup.send(f"[ERROR] 이미지 생성 실패: {e}")

# ================= 7. 비디오 생성 (User Install) =================

# ================= /video 커맨드 — 비용 절감으로 임시 비활성화 =================
# 복원 시 VIDEO_GEN_MODEL 주석 해제 및 아래 코드 복원 필요

# ================= 8. 사운드보드 =================

SOUNDS_PER_PAGE = 25

SB_PLAY_LOG = []  # [{"user": name, "sound": key, "time": str}, ...] 최근 20개
SB_LOG_MAX = 20

class VolumeModal(discord.ui.Modal, title="사운드보드 볼륨 설정"):
    def __init__(self, soundboard_view):
        super().__init__()
        self.soundboard_view = soundboard_view
        self.value = discord.ui.TextInput(
            label="볼륨 (1~100%)",
            placeholder=str(int(bot.sound_volume * 100)),
            max_length=3,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            percent = max(1, min(100, int(self.value.value)))
        except ValueError:
            await temp_reply(interaction, "숫자를 입력해주세요.")
            return
        bot.set_sound_volume(percent / 100.0)
        self.soundboard_view._refresh_keys()
        self.soundboard_view._update_components()
        await interaction.response.edit_message(embed=self.soundboard_view._make_embed(), view=self.soundboard_view)


@bot.tree.command(name="sb", description="사운드보드 (이름 입력시 재생, 빈 입력시 현재 채널에 통합 패널 생성)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(name="재생할 사운드 이름 (비우면 현재 채널을 패널 채널로 설정)")
async def soundboard(interaction: discord.Interaction, name: str = None):
    if not interaction.guild:
        await temp_reply(interaction, "서버에서만 사용 가능합니다.")
        return

    # 인자 없으면 현재 채널을 패널 채널로 설정하고 통합 패널 준비
    if not name:
        await interaction.response.defer()
        try:
            panel_status = await ensure_control_panel(interaction.guild, interaction.channel, save_target=True)
            await temp_followup(
                interaction,
                f"{interaction.channel.mention}에 통합 패널을 {'갱신' if panel_status == 'updated' else '생성'}했습니다.",
            )
        except Exception as e:
            await temp_followup(interaction, f"[ERROR] 통합 패널 생성 실패: {e}")
        return

    # 인자 있으면 바로 재생
    name = unicodedata.normalize("NFC", name.replace("📁 ", "").strip())

    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice:
        await temp_reply(interaction, "음성 채널에 먼저 접속해주세요.")
        return

    await interaction.response.defer()
    try:
        success, msg = await play_sound_in_vc(interaction.guild, member, name, bot)
        if success:
            SB_PLAY_LOG.append({
                "user": get_server_display_name(interaction.guild, interaction.user),
                "sound": name,
                "time": datetime.now(KST).strftime("%H:%M:%S")
            })
            if len(SB_PLAY_LOG) > SB_LOG_MAX:
                SB_PLAY_LOG.pop(0)
        await temp_followup(interaction, msg)
    except Exception as e:
        await temp_followup(interaction, f"[ERROR] {e}")

@soundboard.autocomplete('name')
async def sb_autocomplete(interaction: discord.Interaction, current: str):
    current = unicodedata.normalize("NFC", current.lower())
    all_keys = sorted(bot.sound_files.keys())
    choices = []
    for key in all_keys:
        if current and current not in key:
            continue
        display = key if len(key) <= 100 else "..." + key[-97:]
        choices.append(app_commands.Choice(name=display, value=key))
    return choices[:25]

class OffsetModal(discord.ui.Modal, title="오프셋 설정"):
    def __init__(self, sibo_view):
        super().__init__()
        self.sibo_view = sibo_view
        cfg = sibo_view._get_cfg()
        current = str(cfg.get("offset", 0.0)) if cfg else "0"
        self.value = discord.ui.TextInput(label="정각 몇 초 전에 재생 (소수점 가능, 0~30)", placeholder=current, max_length=10)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            offset = max(0.0, min(30.0, float(self.value.value)))
            gid = self.sibo_view.guild_id
            cfg = bot.get_sibo(gid)
            if not cfg or not cfg.get("sound"):
                await temp_reply(interaction, "먼저 음원을 선택해주세요.")
                return
            cfg["offset"] = offset
            bot.save_config()
            restart_sibo()
            self.sibo_view._update_components()
            await interaction.response.edit_message(embed=self.sibo_view._make_embed(), view=self.sibo_view)
        except ValueError:
            await temp_reply(interaction, "숫자를 입력해주세요.")

class CompModal(discord.ui.Modal, title="딜레이 보정값 설정"):
    def __init__(self, sibo_view):
        super().__init__()
        self.sibo_view = sibo_view
        self.value = discord.ui.TextInput(label="보정 초 (소수점 가능, 음수도 가능)", placeholder=str(SIBO_LATENCY_COMP), max_length=10)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        global SIBO_LATENCY_COMP
        try:
            SIBO_LATENCY_COMP = float(self.value.value)
            g = bot.server_configs.setdefault("_global", {})
            g["sibo_comp"] = SIBO_LATENCY_COMP
            bot.save_config()
            # comp는 다음 사이클에서 자연 반영, restart 불필요
            self.sibo_view._update_components()
            await interaction.response.edit_message(embed=self.sibo_view._make_embed(), view=self.sibo_view)
        except ValueError:
            await temp_reply(interaction, "숫자를 입력해주세요.")


class ControlPanelView(discord.ui.View):
    def __init__(self, guild_id, mode="soundboard"):
        super().__init__(timeout=None)
        self.guild_id = str(guild_id)
        self.mode = mode
        self.folder = ""
        self.search = ""
        self.page = 0
        self.show_log = False
        self.sibo_folder = ""
        self.sound_page = 0
        self._refresh_keys()
        self._rebuild_sound_keys()
        self._update_components()

    def _get_cfg(self):
        return bot.get_sibo(self.guild_id)

    def _refresh_keys(self):
        all_keys = sorted(bot.sound_files.keys())
        if self.folder:
            all_keys = [k for k in all_keys if k.startswith(self.folder + "/")]
        if self.search:
            all_keys = [k for k in all_keys if self.search in k]
        self.original_keys = sorted(bot.sound_files.keys())
        self.all_keys = all_keys
        self.total_pages = max(1, (len(all_keys) + SOUNDS_PER_PAGE - 1) // SOUNDS_PER_PAGE)
        self.page = min(self.page, self.total_pages - 1)

    def _page_keys(self):
        start = self.page * SOUNDS_PER_PAGE
        return self.all_keys[start:start + SOUNDS_PER_PAGE]

    def _rebuild_sound_keys(self):
        all_keys = sorted(bot.sound_files.keys())
        if self.sibo_folder:
            self.sound_keys = [k for k in all_keys if k.startswith(self.sibo_folder + "/")]
        else:
            self.sound_keys = all_keys
        self.sound_total_pages = max(1, (len(self.sound_keys) + 24) // 25)
        self.sound_page = min(self.sound_page, self.sound_total_pages - 1)

    def _page_sound_keys(self):
        start = self.sound_page * 25
        return self.sound_keys[start:start + 25]

    def _make_embed(self):
        if self.mode == "sibo":
            return self._make_sibo_embed()
        return self._make_soundboard_embed()

    def _make_soundboard_embed(self):
        if self.show_log:
            embed = discord.Embed(title="🎛 Gemini 패널 — 사운드보드 로그", color=0x5865F2)
            if SB_PLAY_LOG:
                lines = [f"`{e['time']}` **{e['user']}** → `{e['sound']}`" for e in reversed(SB_PLAY_LOG)]
                embed.description = "\n".join(lines)
            else:
                embed.description = "(재생 기록 없음)"
            embed.set_footer(text=f"최근 {len(SB_PLAY_LOG)}개 | 시보 설정 버튼으로 전환")
            return embed

        page_keys = self._page_keys()
        folder_label = self.folder or "전체"
        listing = "\n".join(f"`{k}`" for k in page_keys) or "(비어있음)"
        embed = discord.Embed(title=f"🎛 Gemini 패널 — 사운드보드/{folder_label}", description=listing, color=0x5865F2)
        if SB_PLAY_LOG:
            last = SB_PLAY_LOG[-1]
            embed.set_footer(text=f"총 {len(self.all_keys)}개 | 페이지 {self.page + 1}/{self.total_pages} | 최근: {last['user']} → {last['sound']}")
        else:
            embed.set_footer(text=f"총 {len(self.all_keys)}개 | 페이지 {self.page + 1}/{self.total_pages}")
        return embed

    def _make_sibo_embed(self):
        cfg = self._get_cfg()
        embed = discord.Embed(title="🎛 Gemini 패널 — 시보 설정", color=0xF1C40F)
        if cfg:
            sound = cfg.get("sound", "없음")
            offset = cfg.get("offset", 0)
            vc_id = cfg.get("vc_channel")
            vc_text = f"<#{vc_id}>" if vc_id else "미설정"
            embed.add_field(name="음원", value=f"`{sound}`", inline=True)
            embed.add_field(name="오프셋", value=f"정각 {offset}초 전", inline=True)
            embed.add_field(name="VC 채널", value=vc_text, inline=True)
            embed.add_field(name="딜레이 보정", value=f"{SIBO_LATENCY_COMP}초", inline=True)
            embed.add_field(name="상태", value="✅ 활성", inline=True)
        else:
            embed.description = "시보가 설정되어 있지 않습니다."
            embed.add_field(name="딜레이 보정", value=f"{SIBO_LATENCY_COMP}초", inline=True)
        folder_label = self.sibo_folder or "전체"
        embed.set_footer(text=f"시보 음원 폴더: {folder_label} | {len(self.sound_keys)}개 | 페이지 {self.sound_page + 1}/{self.sound_total_pages}")
        return embed

    def _update_components(self):
        self.clear_items()
        if self.mode == "sibo":
            self._add_sibo_components()
        else:
            self._add_soundboard_components()

    def _folder_options(self, current_folder):
        folders = sorted(set(k.split("/")[0] for k in bot.sound_files.keys() if "/" in k))
        if not folders:
            return []
        options = [discord.SelectOption(label="📂 전체", value="_all", default=current_folder == "")]
        for folder in folders[:24]:
            options.append(discord.SelectOption(label=f"📁 {folder}", value=folder, default=current_folder == folder))
        return options

    def _add_soundboard_components(self):
        folder_options = self._folder_options(self.folder)
        if folder_options:
            folder_select = discord.ui.Select(placeholder="📁 폴더 선택", options=folder_options, row=0)
            folder_select.callback = self._soundboard_folder_callback
            self.add_item(folder_select)

        page_keys = self._page_keys()
        if page_keys:
            sound_options = []
            for key in page_keys:
                display = key.split("/")[-1] if "/" in key else key
                sound_options.append(discord.SelectOption(label=display[:100], value=key, description=key[:100] if "/" in key else None))
            sound_select = discord.ui.Select(placeholder="🔊 재생할 사운드 선택", options=sound_options, row=1)
            sound_select.callback = self._play_callback
            self.add_item(sound_select)

        if self.total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=2)
            prev_btn.callback = self._prev_callback
            self.add_item(prev_btn)
            page_btn = discord.ui.Button(label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.primary, disabled=True, row=2)
            self.add_item(page_btn)
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=2)
            next_btn.callback = self._next_callback
            self.add_item(next_btn)

        leave_btn = discord.ui.Button(label="🚪 퇴장", style=discord.ButtonStyle.danger, row=2)
        leave_btn.callback = self._leave_callback
        self.add_item(leave_btn)

        log_label = "📋 목록" if self.show_log else "📋 로그"
        log_btn = discord.ui.Button(label=log_label, style=discord.ButtonStyle.primary if self.show_log else discord.ButtonStyle.secondary, row=3)
        log_btn.callback = self._log_callback
        self.add_item(log_btn)

        volume_btn = discord.ui.Button(label=f"🔉 볼륨: {int(bot.sound_volume * 100)}%", style=discord.ButtonStyle.secondary, row=3)
        volume_btn.callback = self._volume_callback
        self.add_item(volume_btn)

        sibo_btn = discord.ui.Button(label="⏰ 시보 설정", style=discord.ButtonStyle.secondary, row=4)
        sibo_btn.callback = self._show_sibo_callback
        self.add_item(sibo_btn)

    def _add_sibo_components(self):
        cfg = self._get_cfg()
        folder_options = self._folder_options(self.sibo_folder)
        if folder_options:
            folder_select = discord.ui.Select(placeholder="📁 폴더 필터", options=folder_options, row=0)
            folder_select.callback = self._sibo_folder_callback
            self.add_item(folder_select)

        page_keys = self._page_sound_keys()
        if page_keys:
            current_sound = cfg.get("sound") if cfg else None
            options = []
            for key in page_keys:
                desc = key[:100] if "/" in key else None
                short = key.split("/")[-1] if "/" in key else key
                options.append(discord.SelectOption(label=short[:100], value=key, description=desc, default=key == current_sound))
            sound_select = discord.ui.Select(placeholder="🔊 시보 음원 선택", options=options, row=1)
            sound_select.callback = self._sound_callback
            self.add_item(sound_select)

        if self.sound_total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.sound_page == 0, row=2)
            prev_btn.callback = self._sound_prev
            self.add_item(prev_btn)
            page_btn = discord.ui.Button(label=f"{self.sound_page + 1}/{self.sound_total_pages}", style=discord.ButtonStyle.primary, disabled=True, row=2)
            self.add_item(page_btn)
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.sound_page >= self.sound_total_pages - 1, row=2)
            next_btn.callback = self._sound_next
            self.add_item(next_btn)

        current_offset = cfg.get("offset", 0) if cfg else 0
        offset_btn = discord.ui.Button(label=f"⏱️ 오프셋: {current_offset}s", style=discord.ButtonStyle.primary, row=3)
        offset_btn.callback = self._offset_modal_callback
        self.add_item(offset_btn)

        comp_btn = discord.ui.Button(label=f"⚙️ 보정: {SIBO_LATENCY_COMP}s", style=discord.ButtonStyle.secondary, row=3)
        comp_btn.callback = self._comp_callback
        self.add_item(comp_btn)

        vc_btn = discord.ui.Button(label="🔗 현재 VC 연결", style=discord.ButtonStyle.success, row=3)
        vc_btn.callback = self._vc_callback
        self.add_item(vc_btn)

        if cfg:
            off_btn = discord.ui.Button(label="❌ 비활성화", style=discord.ButtonStyle.danger, row=3)
            off_btn.callback = self._off_callback
            self.add_item(off_btn)

        sb_btn = discord.ui.Button(label="🔊 사운드보드", style=discord.ButtonStyle.secondary, row=4)
        sb_btn.callback = self._show_soundboard_callback
        self.add_item(sb_btn)

    async def _show_sibo_callback(self, interaction: discord.Interaction):
        self.mode = "sibo"
        self.show_log = False
        self._rebuild_sound_keys()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _show_soundboard_callback(self, interaction: discord.Interaction):
        self.mode = "soundboard"
        self._refresh_keys()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _soundboard_folder_callback(self, interaction: discord.Interaction):
        folder = interaction.data["values"][0]
        self.search = ""
        self.folder = "" if folder == "_all" else folder
        self.page = 0
        self._refresh_keys()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _play_callback(self, interaction: discord.Interaction):
        sound_key = interaction.data["values"][0]
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice:
            await temp_reply(interaction, "음성 채널에 먼저 접속해주세요.")
            return
        try:
            success, _ = await play_sound_in_vc(interaction.guild, member, sound_key, bot)
            if success:
                SB_PLAY_LOG.append({
                    "user": get_server_display_name(interaction.guild, interaction.user),
                    "sound": sound_key,
                    "time": datetime.now(KST).strftime("%H:%M:%S")
                })
                if len(SB_PLAY_LOG) > SB_LOG_MAX:
                    SB_PLAY_LOG.pop(0)
            self._refresh_keys()
            self._update_components()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)
        except Exception as e:
            await temp_reply(interaction, f"[ERROR] {e}")

    async def _log_callback(self, interaction: discord.Interaction):
        self.show_log = not self.show_log
        self._refresh_keys()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _prev_callback(self, interaction: discord.Interaction):
        self._refresh_keys()
        self.page = max(0, self.page - 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _next_callback(self, interaction: discord.Interaction):
        self._refresh_keys()
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _leave_callback(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            actor_name = get_server_display_name(interaction.guild, interaction.user)
            log_voice_snapshot("수동 퇴장 요청", interaction.guild, vc, extra=f"user={actor_name}")
            bot.clear_voice_target(interaction.guild.id)
            await vc.disconnect()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)
        else:
            await temp_reply(interaction, "현재 음성 채널에 접속되어 있지 않습니다.")

    async def _volume_callback(self, interaction: discord.Interaction):
        modal = VolumeModal(self)
        await interaction.response.send_modal(modal)

    async def _sibo_folder_callback(self, interaction: discord.Interaction):
        folder = interaction.data["values"][0]
        self.sibo_folder = "" if folder == "_all" else folder
        self.sound_page = 0
        self._rebuild_sound_keys()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _sound_prev(self, interaction: discord.Interaction):
        self.sound_page = max(0, self.sound_page - 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _sound_next(self, interaction: discord.Interaction):
        self.sound_page = min(self.sound_total_pages - 1, self.sound_page + 1)
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _sound_callback(self, interaction: discord.Interaction):
        sound_key = interaction.data["values"][0]
        cfg = bot.get_sibo(self.guild_id) or {}
        vc_channel = cfg.get("vc_channel")
        offset = cfg.get("offset", 0.0)
        if not vc_channel:
            member = interaction.guild.get_member(interaction.user.id)
            if member and member.voice:
                vc_channel = member.voice.channel.id
        bot.set_sibo(self.guild_id, {"sound": sound_key, "offset": offset, "vc_channel": vc_channel})
        restart_sibo()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _offset_modal_callback(self, interaction: discord.Interaction):
        modal = OffsetModal(self)
        await interaction.response.send_modal(modal)

    async def _comp_callback(self, interaction: discord.Interaction):
        modal = CompModal(self)
        await interaction.response.send_modal(modal)

    async def _vc_callback(self, interaction: discord.Interaction):
        cfg = bot.get_sibo(self.guild_id)
        if not cfg:
            await temp_reply(interaction, "먼저 음원을 선택해주세요.")
            return
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice:
            await temp_reply(interaction, "음성 채널에 먼저 접속해주세요.")
            return
        cfg["vc_channel"] = member.voice.channel.id
        bot.save_config()
        restart_sibo()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _off_callback(self, interaction: discord.Interaction):
        bot.del_sibo(self.guild_id)
        restart_sibo()
        self._update_components()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def on_timeout(self):
        self.clear_items()


# ================= Deep Research 명령어 =================

async def run_deep_research_command(interaction: discord.Interaction, prompt: str, agent_key: str):
    """대화 컨텍스트 없이 입력 프롬프트만 단독 리서치. 채널 락을 잡지 않으므로 일반 대화와 병렬로 돈다."""
    agent_id = DEEP_RESEARCH_AGENTS[agent_key]
    log_time = datetime.now().strftime('%H:%M:%S')
    location = f"[{interaction.guild.name}]" if interaction.guild else "[DM]"
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    print(f"[{log_time}] {location} [/{agent_key}] {actor_name}: {prompt}")

    await interaction.response.send_message(
        f"[Deep Research] `{agent_id}` 작업을 시작했습니다.\n"
        f"**주제:** {prompt[:300]}\n"
        f"완료까지 10분 안팎 걸릴 수 있습니다. 완료되면 멘션으로 알려드립니다."
    )

    async def send_result(content, *, pdf=None, filename=None):
        try:
            if pdf:
                await interaction.followup.send(content, file=discord.File(fp=io.BytesIO(pdf), filename=filename))
            else:
                await interaction.followup.send(content)
        except Exception as followup_error:
            # followup 토큰 만료(15분) 등 — 채널 직접 전송으로 폴백
            print(f"[DEEP] followup 실패, 채널 전송으로 폴백: {followup_error}")
            if interaction.channel is None:
                raise
            if pdf:
                await interaction.channel.send(content, file=discord.File(fp=io.BytesIO(pdf), filename=filename))
            else:
                await interaction.channel.send(content)

    try:
        result = await call_deep_research(prompt, agent_id)
        mention = interaction.user.mention

        if isinstance(result, dict) and result.get("kind") == "deep_research_report":
            await send_result(
                f"{mention} {result.get('summary') or '[Deep Research] 완료했습니다.'}",
                pdf=result.get("pdf_bytes"),
                filename=result.get("filename") or "deep_research_report.pdf",
            )
            print(f"  [BOT]({agent_id}): [Deep Research PDF]")
        else:
            text = f"{mention} {result if isinstance(result, str) else str(result)}"
            for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
                await send_result(chunk)
            print(f"  [BOT]({agent_id}): {str(result)[:200]}")
        print("-------")
    except Exception as e:
        print(f"  [ERROR] /{agent_key}: {e}")
        print("-------")


@bot.tree.command(name="deepo", description="Deep Research — 입력한 주제만 단독 리서치 (약 10분, PDF 리포트)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(prompt="리서치할 주제/질문 (대화 컨텍스트는 참조하지 않음)")
async def deepo_command(interaction: discord.Interaction, prompt: str):
    await run_deep_research_command(interaction, prompt, "deepo")


@bot.tree.command(name="deepm", description="Deep Research Max — 입력한 주제만 단독 리서치 (더 깊게, 더 오래 걸림)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(prompt="리서치할 주제/질문 (대화 컨텍스트는 참조하지 않음)")
async def deepm_command(interaction: discord.Interaction, prompt: str):
    await run_deep_research_command(interaction, prompt, "deepm")


# ================= 모델 전환 =================

@bot.tree.command(name="model", description="AI 모델 전환")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="모델 선택")
async def switch_model(interaction: discord.Interaction, name: str = None):
    global TEXT_MODEL, IMAGE_MODEL
    if name is None:
        if interaction.guild:
            cfg = bot.get_config(interaction.guild_id)
            server_tm = cfg.get("text_model", TEXT_MODEL)
            server_im = cfg.get("image_model", IMAGE_MODEL)
            server_hist = get_history_limit(cfg)
            server_image_hist = get_image_history_limit(cfg)
            await interaction.response.send_message(
                f"이 서버 모델: `{server_tm}` (텍스트, 히스토리 {server_hist}개, 이미지 {server_image_hist}개) / `{server_im}` (이미지)\n"
                f"글로벌 기본값: `{TEXT_MODEL}` / `{IMAGE_MODEL}`"
            )
        else:
            await interaction.response.send_message(
                f"글로벌 기본 모델: `{TEXT_MODEL}` / `{IMAGE_MODEL}` (이미지)"
            )
        return
    if name not in AVAILABLE_MODELS:
        available = ', '.join(AVAILABLE_MODELS.keys())
        await temp_reply(interaction, f"'{name}' 모델을 찾을 수 없습니다. 사용 가능: {available}")
        return
    TEXT_MODEL = AVAILABLE_MODELS[name]["text"]
    IMAGE_MODEL = AVAILABLE_MODELS[name]["image"]
    thinking_level = AVAILABLE_MODELS[name].get("thinking_level")  # None이면 모델 default
    if interaction.guild:
        cfg = bot.get_config(interaction.guild_id)
        cfg["text_model"] = TEXT_MODEL
        cfg["image_model"] = IMAGE_MODEL
        # 레거시 키 정리 (thinking_budget은 deprecated)
        cfg.pop("thinking_budget", None)
        if thinking_level is None:
            cfg.pop("thinking_level", None)
        else:
            cfg["thinking_level"] = thinking_level
        bot.save_config()
    history_limit = get_history_limit(bot.get_config(interaction.guild_id)) if interaction.guild else DEFAULT_HISTORY_LIMIT
    image_history_limit = get_image_history_limit(bot.get_config(interaction.guild_id)) if interaction.guild else DEFAULT_IMAGE_HISTORY_LIMIT
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    thinking_label = f"`{thinking_level}`" if thinking_level else "모델 기본값"
    await interaction.response.send_message(
        f"모델 변경됨: **{name}** by {actor_name}\n"
        f"텍스트: `{TEXT_MODEL}`\n이미지: `{IMAGE_MODEL}`\n"
        f"사고 수준: {thinking_label}\n히스토리 참조: 텍스트 {history_limit}개 / 이미지 {image_history_limit}개"
    )

@switch_model.autocomplete('name')
async def model_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(
            name=key,
            value=key
        )
        for key, info in AVAILABLE_MODELS.items()
        if current.lower() in key.lower() or current.lower() in info['text'].lower()
    ][:25]

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
