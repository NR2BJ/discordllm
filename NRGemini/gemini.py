import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai
from google.genai import types
from bs4 import BeautifulSoup
import json
import os
import asyncio
import aiohttp
import io
import warnings
import re
import tempfile
import time as time_module
from pathlib import Path
from PIL import Image
from datetime import datetime, timedelta, timezone
import glob as glob_module
import unicodedata

warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.genai._api_client")

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
    "3.1-pro": {
        "text": "gemini-3.1-pro-preview",
        "image": "gemini-3.1-flash-image-preview",
    },
    "3-flash": {
        "text": "gemini-3-flash-preview",
        "image": "gemini-3.1-flash-image-preview",
    },
    "3.1-flash-lite": {
        "text": "gemini-3.1-flash-lite-preview",
        "image": "gemini-3.1-flash-image-preview",
    },
}
TEXT_MODEL = AVAILABLE_MODELS["3.1-pro"]["text"]
IMAGE_MODEL = AVAILABLE_MODELS["3.1-pro"]["image"]
# VIDEO_GEN_MODEL = "veo-3.1-generate-preview"  # 비용 절감으로 임시 비활성화

HISTORY_LIMIT = 100
IMAGE_SEARCH_LIMIT = 20
MAX_REF_IMAGES = 1

_COMMON_RULES = """## General Rules
- Output response content directly. No prefixes like 'Bot:' or 'Model:'.
- Respond in the user's language and mirror their tone (casual, formal, vulgar — match them).
- If the user sets up a specific roleplay scenario (character, world, setting), maintain it consistently within that conversation. Do NOT treat casual chat as "established context" that must persist.
- If you see `[System Error]` or `[Error]` in history, YOUR previous action failed.
- When a URL is in the message, the system fetches it and attaches `[Webpage Content]` — use that content directly.
- Analyze attached files (images/video/audio) when supported.
- Keep responses concise and Discord-appropriate.
- Use Google Grounding Search (Web Search) actively since your training cutoff may be outdated."""

SYSTEM_PROMPT = f"""
You are an AI member of this Discord server.

## User Identification
- Each user message is prefixed with [사용자:display_name|ID:numeric_id].
- The numeric ID is a UNIQUE identifier. Two users with similar-looking names are ALWAYS different people if their IDs differ.
- NEVER merge or conflate messages from different IDs, even if topics are similar or names look alike.
- Track each user's statements separately. What User A said is not what User B said.
- When responding, address users by their display_name (ignore the ID in your output).

## Conversation Context
- Chat history is for CONTEXT ONLY — to understand the flow, not to recycle.
- When the topic changes, focus ONLY on the current topic. Do NOT drag in old topics.
- Do NOT reuse specific words, jokes, memes, or references from previous conversations to fake familiarity. Every new topic gets a fresh start.
- User's background info (job, interests, etc.) is for matching TONE only. Do NOT shoehorn it into answers unless the user explicitly brings it up or it has direct logical relevance to the current question.
- Answer new questions cleanly and on-point. Do NOT over-decorate answers by mixing in past context.
- You may briefly reference a previous topic AT MOST once if naturally relevant, then move on entirely.

{_COMMON_RULES}

## Image Generation Tool (`generate_image`)
- Use this tool when the user wants visual content (image/picture/illustration).
- `ref_count` — how many previous images to reference:
  - 0 = new image (DEFAULT — use this unless the user is clearly asking to modify the existing image)
  - 1 = modify/refine the most recent image (use ONLY when user explicitly asks to change/adjust/modify the existing image, e.g. "수정해줘", "좀 더 X하게 바꿔줘")
- NEVER write image placeholders like `[이미지 첨부됨]` or `(Bot generated an image here)` as your reply. Always call the tool.
- If the tool is unavailable, say so explicitly.
"""

CHAT_SYSTEM_PROMPT = f"""
You are a helpful AI assistant.
{_COMMON_RULES}
- If an image is provided, analyze it carefully and respond based on both the image and the user's message.
"""

IMAGE_SYSTEM_PROMPT = f"""
You are an AI image generator assistant.
{_COMMON_RULES}
- When the user wants to generate or edit an image, use the `generate_image` tool.
- Create a detailed English prompt for the image based on the user's request.
- You can also provide a brief text response along with generating the image.
- If a reference image is provided, set ref_count to 1 for editing.
"""

# 현재 파일(gemini.py)이 있는 폴더 경로를 구함
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
KST = timezone(timedelta(hours=9))

SOUND_DIR = os.path.join(BASE_DIR, "sb")
SOUND_EXTENSIONS = ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.opus', '.weba')
os.makedirs(SOUND_DIR, exist_ok=True)
os.makedirs(os.path.join(SOUND_DIR, "discord"), exist_ok=True)
SOUND_VOLUME = 0.15  # 기본 볼륨 (0.0~1.0)
SIBO_LATENCY_COMP = 0.0  # Discord 전송 지연 보정 (초)
SIBO_PRELOAD_LEAD = 0.3  # 정각 전 ffmpeg 미리 띄워 파이프 버퍼 채우는 시간 (초)
VOICE_RECONNECT_DELAY = 3.0
VOICE_RECONNECT_COOLDOWN = 20.0

class SilenceAudio(discord.AudioSource):
    """ffmpeg 없이 무음 PCM 스트림 생성. opus 크래시 방지."""
    def read(self):
        # 20ms of silence at 48kHz stereo 16-bit = 3840 bytes
        return b'\x00' * 3840
    def is_opus(self):
        return False
    def cleanup(self):
        pass

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

def describe_voice_client(vc):
    if vc is None:
        return "vc=None"
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
    return (
        f"connected={connected} playing={playing} paused={paused} "
        f"channel={format_channel_ref(getattr(vc, 'channel', None))} "
        f"endpoint={endpoint} session_id={session_id} "
        f"latency={format_metric(getattr(vc, 'latency', None))} "
        f"avg_latency={format_metric(getattr(vc, 'average_latency', None))}"
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
    parts = [f"[VC] {prefix}"]
    if guild is not None:
        parts.append(f"guild={guild.name}({guild.id})")
    if extra:
        parts.append(extra)
    parts.append(describe_voice_client(vc))
    print(" | ".join(parts))

def play_silence(vc, reason="unknown"):
    """VC에 무음 스트림 재생. 이미 재생 중이면 스킵."""
    if vc.is_connected() and not vc.is_playing():
        vc.play(SilenceAudio())
        log_voice_snapshot(f"무음 재생 시작 [{reason}]", getattr(vc, "guild", None), vc)

async def cleanup_stale_voice_client(vc, guild):
    """끊어진 VoiceClient 캐시를 정리해서 재접속 막힘 방지."""
    log_voice_snapshot("stale VC 정리 시작", guild, vc)
    try:
        await vc.disconnect(force=True)
    except TypeError:
        try:
            await vc.disconnect()
        except Exception as e:
            print(f"[VC] stale disconnect fallback 실패 ({guild.name}): {e}")
    except Exception as e:
        print(f"[VC] stale disconnect 실패 ({guild.name}): {e}")
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
            print(f"[VC] 채널 조회 실패 ({guild.name}, {channel_id}): {e}")
            return None
    if not hasattr(channel, "connect"):
        print(f"[VC] 연결 불가 채널 타입 ({guild.name}, {channel_id})")
        return None
    return channel

async def ensure_voice_connection(guild, channel_id, *, move=True, start_silence=False, reason="unknown"):
    channel = await resolve_voice_channel(guild, channel_id)
    if channel is None:
        print(f"[VC] 연결 대상 채널 조회 실패 ({guild.name}, {channel_id}) [{reason}]")
        return None

    async with bot.get_voice_lock(guild.id):
        vc = guild.voice_client
        log_voice_snapshot(
            f"ensure_voice_connection 시작 [{reason}]",
            guild,
            vc,
            extra=f"target={format_channel_ref(channel)} move={move} start_silence={start_silence}",
        )
        if vc and vc.is_connected():
            if move and vc.channel != channel:
                old_channel = vc.channel
                await vc.move_to(channel)
                log_voice_snapshot(
                    f"이동 완료 [{reason}]",
                    guild,
                    vc,
                    extra=f"from={format_channel_ref(old_channel)} to={format_channel_ref(channel)}",
                )
            if start_silence and not vc.is_playing():
                play_silence(vc, reason=f"{reason}:connected")
            return vc

        if vc:
            await cleanup_stale_voice_client(vc, guild)

        try:
            vc = await channel.connect(reconnect=True)
        except TypeError:
            vc = await channel.connect()

        log_voice_snapshot(
            f"연결 완료 [{reason}]",
            guild,
            vc,
            extra=f"target={format_channel_ref(channel)}",
        )
        if start_silence and not vc.is_playing():
            play_silence(vc, reason=f"{reason}:fresh_connect")
        return vc

# ================= 사운드보드 유틸리티 =================

def scan_sound_files():
    """재귀 스캔. 키: 상대경로(확장자 제외, 소문자). 값: 절대경로."""
    sounds = {}
    for root, dirs, files in os.walk(SOUND_DIR):
        for f in files:
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
        start_silence=False,
        reason="soundboard",
    )
    if vc is None:
        return False, "음성 채널 연결에 실패했습니다."

    if vc.is_playing():
        vc.stop()

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
        async def start_silence():
            play_silence(vc, reason=f"after_play:{key}")
        asyncio.run_coroutine_threadsafe(start_silence(), bot_instance.loop)

    vc.play(audio, after=after_play)
    return True, f"**{sound_name}** 재생 중 (vol {int(bot_instance.sound_volume * 100)}%)"

# ================= 2. 관리자 클래스 =================
class DataManager:
    @staticmethod
    def load_json(filename, default_data):
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
            except: pass
        return default_data

    @staticmethod
    def save_json(filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
        except: pass

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
                    return clean_text[:12000] + ("..." if len(clean_text) > 12000 else "")
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
            coro = CLIENT.aio.models.generate_content(
                model=image_model,
                contents=contents
            )
            response = await asyncio.wait_for(coro, timeout=90.0)

            if not response.candidates: raise Exception("No response candidate")
            candidate = response.candidates[0]
            if candidate.finish_reason != "STOP": raise Exception(f"Blocked: {candidate.finish_reason}")

            for part in candidate.content.parts:
                if part.inline_data: return part.inline_data.data

            raise Exception("No image data found in response")

        except Exception as e:
            error_msg = str(e)
            if "500" in error_msg or "Payload" in error_msg or "Connection" in error_msg or "400" in error_msg or "Timeout" in error_msg:
                print(f"[WARN] 이미지 생성 실패 (시도 {attempt+1}/{max_retries}): {error_msg} -> 5초 후 재시도...")
                await asyncio.sleep(5)
                if attempt == max_retries - 1: raise e
            else:
                raise e

async def check_media_intent(last_user_msg, text_model=None):
    """사용자가 이미지/동영상 생성을 원하는지 판별. 'IMAGE', 'VIDEO', 또는 'NONE' 반환"""
    if not CLIENT: return "NONE"
    if text_model is None:
        text_model = TEXT_MODEL
    prompt = f"""
    Analyze the user's request: "{last_user_msg}"
    Determine if the user explicitly wants the AI to:
    - GENERATE, DRAW, EDIT, or MODIFY an IMAGE → return "IMAGE"
    - GENERATE or CREATE a VIDEO/ANIMATION/CLIP → return "VIDEO"
    - Neither → return "NONE"
    Return ONLY one word: "IMAGE", "VIDEO", or "NONE".
    """
    try:
        response = await CLIENT.aio.models.generate_content(
            model=text_model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        result = response.text.strip().upper()
        if "VIDEO" in result: return "VIDEO"
        if "IMAGE" in result: return "IMAGE"
        return "NONE"
    except:
        return "NONE"

async def call_gemini(history_contents, last_user_msg="", text_model=None):
    if not CLIENT: return "Error: API Client not initialized", 0
    if text_model is None:
        text_model = TEXT_MODEL

    media_gen_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="generate_image",
                description="Generate, edit, or mix images based on user request.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "prompt": types.Schema(type="STRING", description="Detailed English description for the image."),
                        "ref_count": types.Schema(type="INTEGER", description="0 = generate new, 1 = edit the most recent image. Only 0 or 1 allowed.")
                    },
                    required=["prompt", "ref_count"]
                )
            ),
            # generate_video — 비용 절감으로 임시 비활성화
        ]
    )

    media_intent = await check_media_intent(last_user_msg, text_model=text_model)

    if media_intent in ("IMAGE", "VIDEO"):
        tools = [media_gen_tool]
        tool_config = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO"))

        last_content = history_contents[-1]
        if last_content.role == "user":
            hint = "an image" if media_intent == "IMAGE" else "a video"
            tool_name = "generate_image" if media_intent == "IMAGE" else "generate_video"
            for part in last_content.parts:
                if part.text:
                    part.text += f"\n\n(System: User wants {hint}. Use `{tool_name}`. You are encouraged to provide a text response as well.)"
                    break
    else:
        tools = [types.Tool(google_search=types.GoogleSearch())]
        tool_config = None

    safety_settings = [
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]

    try:
        response = await CLIENT.aio.models.generate_content(
            model=text_model,
            contents=history_contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=tools,
                temperature=0.7,
                tool_config=tool_config,
                safety_settings=safety_settings
            )
        )

        usage = 0
        if response.usage_metadata:
            usage = response.usage_metadata.total_token_count

        return response, usage

    except Exception as e:
        return f"Error: {e}", 0

# ================= 4. 봇 메인 로직 =================
class GeminiBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}
        self.voice_locks = {}
        self.voice_retry_after = {}
        self.sound_files = scan_sound_files()
        self.sound_volume = SOUND_VOLUME
        self._sound_dir_mtime = self._get_sound_dir_mtime()
        global SIBO_LATENCY_COMP
        SIBO_LATENCY_COMP = self.server_configs.get("_global", {}).get("sibo_comp", SIBO_LATENCY_COMP)
        self.active_views = []  # View 참조 유지용

    def _get_sound_dir_mtime(self):
        """SOUND_DIR 전체의 최신 mtime 반환."""
        latest = 0
        for root, dirs, files in os.walk(SOUND_DIR):
            for name in files + dirs:
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
            self.server_configs[gid] = {"channel_id": None, "reset_time": None}
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

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)

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
            print(f"[VC] keepalive 대상 저장 ({guild_name}): channel_id={channel_id}")

    def clear_voice_target(self, guild_id):
        cfg = self.get_config(guild_id)
        if "voice_target_channel_id" in cfg:
            old_channel_id = cfg.get("voice_target_channel_id")
            cfg.pop("voice_target_channel_id", None)
            self.save_config()
            guild = self.get_guild(guild_id)
            guild_name = guild.name if guild else guild_id
            print(f"[VC] keepalive 대상 해제 ({guild_name}): channel_id={old_channel_id}")

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

bot = GeminiBot()

@tasks.loop(seconds=10)
async def voice_watchdog():
    """VC 연결 유지 감시: 무음 재생 유지 + 끊긴 경우 자동 재접속."""
    for guild in bot.guilds:
        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                if not vc.is_playing():
                    play_silence(vc, reason="watchdog")
            except Exception as e:
                print(f"[VC] watchdog 재시작 실패 ({guild.name}): {e}")
            continue

        target_channel_id = bot.get_voice_target(guild.id)
        if not target_channel_id or not bot.can_retry_voice(guild.id):
            continue

        try:
            log_voice_snapshot(
                "watchdog 재접속 시도",
                guild,
                vc,
                extra=f"target_channel_id={target_channel_id}",
            )
            vc = await ensure_voice_connection(
                guild,
                target_channel_id,
                move=True,
                start_silence=True,
                reason="watchdog",
            )
            if vc:
                bot.reset_voice_retry(guild.id)
        except Exception as e:
            print(f"[VC] watchdog 재접속 실패 ({guild.name}): {e}")

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
            if not vc or not vc.is_connected():
                # VC 끊겼으면 저장된 채널로 자동 재접속
                vc_channel_id = cfg.get("vc_channel")
                if vc_channel_id:
                    channel = guild.get_channel(vc_channel_id)
                    if channel:
                        try:
                            vc = await channel.connect()
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

            if vc.is_playing():
                vc.stop()
            def after_sibo(error, _vc=vc):
                async def start_silence():
                    play_silence(_vc)
                asyncio.run_coroutine_threadsafe(start_silence(), bot.loop)
            vc.play(audio, after=after_sibo)
            play_now = datetime.now(KST)
            print(f"[SIBO] {play_now.strftime('%H:%M:%S.%f')[:-4]} Guild {gid} 시보 재생: {sound_key}")

        await asyncio.sleep(1)

async def setup_panel_channels():
    """봇 시작 시 사운드보드 채널 메시지 초기화 후 /sb, /sibo 임베드 자동 재생성."""
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
            deleted = await channel.purge(limit=500, bulk=False)
            print(f"[PANEL] {guild.name}: #{channel.name} {len(deleted)}개 메시지 삭제")
        except Exception as e:
            print(f"[PANEL] {guild.name}: 채널 초기화 실패: {e}")
            continue
        try:
            # /sibo 임베드
            sibo_view = SiboView(guild.id)
            bot.active_views.append(sibo_view)
            await channel.send(embed=sibo_view._make_embed(), view=sibo_view)
            # /sb 임베드
            keys = sorted(bot.sound_files.keys())
            if keys:
                sb_view = SoundboardView(keys)
                bot.active_views.append(sb_view)
                await channel.send(embed=sb_view._make_embed(), view=sb_view)
            print(f"[PANEL] {guild.name}: 패널 임베드 재생성 완료")
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
    if not voice_watchdog.is_running():
        voice_watchdog.start()
    if not sound_watcher.is_running():
        sound_watcher.start()
    if not hasattr(bot, '_sibo_task') or bot._sibo_task is None or bot._sibo_task.done():
        global _sibo_generation
        _sibo_generation += 1
        bot._sibo_task = bot.loop.create_task(sibo_loop(_sibo_generation))
    print(f"[OK] 사운드 감시 + 시보 스케줄러 시작")
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
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
    print(f"[GATEWAY] on_disconnect | active_voice_clients={active}")
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
    print(f"[GATEWAY] on_resumed | active_voice_clients={active}")
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
        vc = member.guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing():
            await asyncio.sleep(1)
            try:
                play_silence(vc, reason="voice_state_update")
            except Exception as e:
                print(f"[VC] 무음 재시작 실패: {e}")
        return

    target_channel_id = bot.get_voice_target(member.guild.id)
    if not target_channel_id:
        return

    print(
        f"[VC] bot 음성 채널 이탈 감지 ({member.guild.name}): "
        f"target_channel_id={target_channel_id}, {VOICE_RECONNECT_DELAY}초 후 재접속 시도"
    )
    await asyncio.sleep(VOICE_RECONNECT_DELAY)
    try:
        vc = await ensure_voice_connection(
            member.guild,
            target_channel_id,
            move=True,
            start_silence=True,
            reason="voice_state_update",
        )
        if vc:
            bot.reset_voice_retry(member.guild.id)
    except Exception as e:
        print(f"[VC] 이벤트 기반 재접속 실패 ({member.guild.name}): {e}")

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
                text_extensions = ('.txt', '.py', '.json', '.md', '.c', '.cpp', '.js', '.html', '.css', '.log', '.xml', '.yaml', '.sh', '.csv')
                reset_ts = datetime.fromisoformat(config["reset_time"]) if config.get("reset_time") else None

                history_list = []

                async for msg in message.channel.history(limit=HISTORY_LIMIT + 10):
                    if msg.id == message.id: continue
                    if msg.id > message.id and msg.author != bot.user: continue
                    if reset_ts and msg.created_at < reset_ts: break

                    if msg.author == bot.user:
                        content = msg.content
                        if '"action":' in content or '"prompt":' in content:
                            content = "[System Log: Action Performed]"
                        elif "Error" in content:
                            content = "[System Error Log]"
                        if msg.attachments:
                            for att in msg.attachments:
                                if att.content_type and att.content_type.startswith('video'):
                                    content += f"\n(Bot generated a video here: {att.filename})"
                                # 이미지는 마커 없이 스킵 (최근 이미지는 base64로 별도 포함)
                        if not content.strip():
                            continue  # 텍스트 없는 이미지 전용 메시지는 히스토리 제외
                    else:
                        _member = message.guild.get_member(msg.author.id) or msg.author
                        content = f"[사용자:{_member.display_name}|ID:{msg.author.id}]: {msg.content}"
                        if msg.attachments:
                            for att in msg.attachments:
                                if att.content_type and (att.content_type.startswith('video') or att.content_type.startswith('audio')):
                                    media_type = att.content_type.split('/')[0]
                                    content += f"\n(User attached a {media_type} file: {att.filename})"
                                else:
                                    is_text = (att.content_type and "text" in att.content_type) or att.filename.lower().endswith(text_extensions)
                                    if is_text:
                                        try:
                                            file_bytes = await att.read()
                                            file_text = file_bytes.decode('utf-8')
                                            content += f"\n\n[Attached File Content: {att.filename}]\n```\n{file_text}\n```"
                                        except: pass

                    role = "model" if msg.author == bot.user else "user"
                    history_list.insert(0, types.Content(role=role, parts=[types.Part(text=content)]))

                _cur_member = message.guild.get_member(message.author.id) or message.author
                current_parts = [types.Part(text=f"[사용자:{_cur_member.display_name}|ID:{message.author.id}]: {message.content}")]

                urls = re.findall(r'(https?://\S+)', message.content)
                for url in urls:
                    web_content = await scrap_webpage(url)
                    if web_content:
                        current_parts.append(types.Part(text=f"\n\n[Webpage Content: {url}]\n```\n{web_content}\n```"))
                        print(f"[WEB] 웹사이트 내용 읽음: {url}")

                if message.attachments:
                    for att in message.attachments:
                        if att.content_type and att.content_type.startswith('image'):
                            d, m = await download_media(att.url)
                            if d: current_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=d.getvalue())))
                        elif att.content_type and (att.content_type.startswith('video') or att.content_type.startswith('audio')):
                            d, m = await download_media(att.url)
                            if d:
                                file_bytes = d.getvalue()
                                media_type = att.content_type.split('/')[0]
                                if len(file_bytes) < 15 * 1024 * 1024:
                                    current_parts.append(types.Part(inline_data=types.Blob(mime_type=m, data=file_bytes)))
                                else:
                                    file_ref = await upload_file_api(file_bytes, m)
                                    if file_ref:
                                        current_parts.append(types.Part(file_data=types.FileData(file_uri=file_ref.uri, mime_type=m)))
                                print(f"[{media_type.upper()}] {media_type} 첨부: {att.filename} ({len(file_bytes)} bytes)")
                        else:
                            is_text = (att.content_type and "text" in att.content_type) or att.filename.lower().endswith(text_extensions)
                            if is_text:
                                try:
                                    file_bytes = await att.read()
                                    file_content = file_bytes.decode('utf-8')
                                    current_parts.append(types.Part(text=f"\n\n[Attached File Content: {att.filename}]\n```\n{file_content}\n```"))
                                except: pass

                full_contents = history_list + [types.Content(role="user", parts=current_parts)]

                # Per-server model selection (falls back to global default)
                server_text_model = config.get("text_model", TEXT_MODEL)
                server_image_model = config.get("image_model", IMAGE_MODEL)

                response, token_usage = await call_gemini(full_contents, message.content, text_model=server_text_model)

                if isinstance(response, str):
                    await message.channel.send(response)
                    return

                if not response.candidates:
                    await message.channel.send("[BLOCKED] 응답이 차단되었습니다.")
                    return

                log_time = datetime.now().strftime('%H:%M:%S')
                print(f"[{log_time}] [{message.guild.name}] {message.author.display_name}: {message.content}")

                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason
                if finish_reason and str(finish_reason) != "STOP":
                    print(f"[WARN] finish_reason: {finish_reason}")

                if not candidate.content or not candidate.content.parts:
                    print(f"[WARN] content.parts가 없음, 응답 스킵 (finish_reason: {finish_reason})")
                    return

                text_reply = ""
                for part in candidate.content.parts:
                    if part.text: text_reply += part.text

                tool_calls = []

                for part in candidate.content.parts:
                    if part.function_call:
                        if part.function_call.name == "generate_image":
                            tool_calls.append(part.function_call)

                fake_json_detected = False
                json_matches = re.findall(r'(\{.*"action":.*"generate_image".*\})', text_reply, re.DOTALL)
                if not json_matches:
                     json_matches = re.findall(r'(\{.*"prompt":.*\})', text_reply, re.DOTALL)

                if json_matches:
                    for json_str in json_matches:
                        try:
                            data = json.loads(json_str)
                            input_args = {}
                            is_valid = False

                            if data.get("action") == "generate_image":
                                raw_input = data.get("action_input", {})
                                if isinstance(raw_input, str):
                                    try: input_args = json.loads(raw_input)
                                    except: pass
                                else:
                                    input_args = raw_input
                                is_valid = True
                            elif "prompt" in data:
                                input_args = data
                                is_valid = True

                            if is_valid:
                                fake_call = type("FakeCall", (), {})()
                                fake_call.name = "generate_image"
                                fake_call.args = input_args
                                tool_calls.append(fake_call)
                                fake_json_detected = True
                                text_reply = text_reply.replace(json_str, "")
                        except: pass

                elif "generate_image(" in text_reply:
                    try:
                        prompt_match = re.search(r'prompt=["\'](.*?)["\']', text_reply, re.DOTALL)
                        ref_match = re.search(r'ref_count=(\d+)', text_reply)
                        if prompt_match:
                            fake_call = type("FakeCall", (), {})()
                            fake_call.name = "generate_image"
                            fake_call.args = {
                                "prompt": prompt_match.group(1),
                                "ref_count": int(ref_match.group(1)) if ref_match else 0
                            }
                            tool_calls.append(fake_call)
                            text_reply = re.sub(r'generate_image\(.*?\)', '', text_reply, flags=re.DOTALL)
                    except: pass

                # 마커를 응답에 출력한 경우 (도구 호출 대신 텍스트로 출력)
                _fake_img_marker = None
                for _m in ["[이미지 첨부됨]", "(Bot generated an image here)"]:
                    if _m in text_reply:
                        _fake_img_marker = _m
                        break
                if _fake_img_marker and not tool_calls:
                    before = text_reply.split(_fake_img_marker)[0].strip()
                    # 백틱 안의 실제 프롬프트 우선 추출
                    backtick_match = re.search(r'`([^`]{15,})`', before, re.DOTALL)
                    if backtick_match:
                        extracted = backtick_match.group(1).strip()
                    else:
                        # 볼드 헤더([OK], **...**) 제거 후 사용
                        extracted = re.sub(r'^\[.*?\]\s*', '', before).strip()
                        extracted = re.sub(r'\*\*[^*]+\*\*:?\s*', '', extracted).strip()
                    # ref_count: 모델이 텍스트에 명시했으면 사용, 아니면 0
                    ref_match = re.search(r'ref[_\s-]*count[:\s=]*(\d+)', before, re.IGNORECASE)
                    ref_count_val = int(ref_match.group(1)) if ref_match else 0
                    if len(extracted) >= 10:
                        fake_call = type("FakeCall", (), {})()
                        fake_call.name = "generate_image"
                        fake_call.args = {"prompt": extracted, "ref_count": ref_count_val}
                        tool_calls.append(fake_call)
                        text_reply = ""  # 이미지로 대체되므로 텍스트 불필요
                        print(f"[FAKE→REAL] '{_fake_img_marker}' 감지, 실제 생성으로 전환: '{extracted[:80]}'")
                    else:
                        text_reply = text_reply.replace(_fake_img_marker, "").strip()

                if text_reply.strip():
                    print(f"[BOT]({server_text_model}): {text_reply} (Tokens: {token_usage}, Finish: {finish_reason})")
                    await send_long_message(message.channel, text_reply)

                for fc in tool_calls:
                    args = fc.args if not isinstance(fc.args, dict) else fc.args
                    prompt = args.get("prompt") if isinstance(args, dict) else args.get("prompt", "image")
                    ref_count = int(args.get("ref_count")) if isinstance(args, dict) else int(args.get("ref_count", 0))
                    # 정책상 0 또는 1만 허용 (2장 이상 블렌딩 미지원)
                    ref_count = max(0, min(ref_count, 1))

                    print(f"[IMG] 생성 요청: '{prompt}' (참조: {ref_count}장)")
                    status_msg = await message.channel.send(f"**Prompt:** `{prompt}`\n(Ref: {ref_count})")

                    ref_images = []
                    if ref_count > 0:
                        found = 0
                        async for msg in message.channel.history(limit=IMAGE_SEARCH_LIMIT, before=message):
                            if reset_ts and msg.created_at < reset_ts: break
                            if msg.attachments:
                                for att in msg.attachments:
                                    if att.content_type.startswith('image'):
                                        d, _ = await download_media(att.url)
                                        if d:
                                            ref_images.append(Image.open(d))
                                            found += 1
                                    if found >= MAX_REF_IMAGES: break
                            if found >= MAX_REF_IMAGES: break

                        if not ref_images:
                            await status_msg.edit(content=f"[WARN] 참조 이미지를 못 찾아 새로 그립니다.\n`{prompt}`")

                    req_content = ref_images + [prompt]
                    try:
                        img_data = await call_nano_banana(req_content, image_model=server_image_model)
                        with io.BytesIO(img_data) as binary:
                            binary.seek(0)
                            fn = "edited.png" if ref_images else "generated.png"
                            await message.channel.send(file=discord.File(fp=binary, filename=fn))

                        await status_msg.edit(content=f"[OK] **Generated Prompt:** `{prompt}`")
                        print(f"[BOT]({server_image_model}): [이미지 생성 완료]")

                    except Exception as e:
                        err_msg = f"[ERROR] Image Generation Failed: {str(e)}"
                        print(err_msg)
                        await status_msg.edit(content=err_msg)

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
    await temp_reply(interaction, "[OK] 설정 완료")

@bot.tree.command(name="reset", description="대화 초기화")
async def reset(interaction: discord.Interaction):
    if not interaction.guild:
        await temp_reply(interaction, "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    bot.save_config()
    await temp_reply(interaction, "[OK] 초기화 완료")

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
    print(f"[{log_time}] {location} [/chat] {interaction.user.display_name}: {message}")

    try:
        parts = [types.Part(text=message)]

        img_data = None
        mime_type = None
        image_source = None

        if image and image.content_type and image.content_type.startswith('image'):
            img_data, mime_type = await download_media(image.url)
            if img_data:
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
        user_display = f"**{interaction.user.display_name}**: {message}"
        if image_source:
            embed = discord.Embed(description=user_display)
            embed.set_image(url=image_source)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(user_display)

        contents = [types.Content(role="user", parts=parts)]

        safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]

        response = await CLIENT.aio.models.generate_content(
            model=TEXT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=CHAT_SYSTEM_PROMPT,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.7,
                safety_settings=safety_settings
            )
        )

        if not response.candidates:
            await interaction.followup.send("[BLOCKED] 응답이 차단되었습니다.")
            return

        text_reply = ""
        for part in response.candidates[0].content.parts:
            if part.text: text_reply += part.text

        token_usage = response.usage_metadata.total_token_count if response.usage_metadata else 0

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
@app_commands.allowed_installs(guilds=True, users=True)
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
    print(f"[{log_time}] {location} [/image] {interaction.user.display_name}: {prompt}")

    try:
        ref_images = []
        has_reference = False
        reference_source = None

        if reference and reference.content_type and reference.content_type.startswith('image'):
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

        # 사용자 입력 표시
        user_display = f"**{interaction.user.display_name}**: {prompt}"
        if reference_source:
            embed = discord.Embed(description=user_display)
            embed.set_image(url=reference_source)
            embed.set_footer(text="원본 이미지")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(user_display)

        gen_tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="generate_image",
                    description="Generate or edit an image based on the prompt.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "prompt": types.Schema(type="STRING", description="Detailed English description for the image."),
                            "ref_count": types.Schema(type="INTEGER", description="0 = generate new, 1 = edit reference image. Only 0 or 1 allowed.")
                        },
                        required=["prompt", "ref_count"]
                    )
                )
            ]
        )

        safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]

        user_message = f"User request: {prompt}"
        if has_reference:
            user_message += "\n(A reference image is provided for editing)"
        user_message += "\n\n(System: Generate the image using the `generate_image` tool. You may also provide a brief text response.)"

        response = await CLIENT.aio.models.generate_content(
            model=TEXT_MODEL,
            contents=[types.Content(role="user", parts=[types.Part(text=user_message)])],
            config=types.GenerateContentConfig(
                system_instruction=IMAGE_SYSTEM_PROMPT,
                tools=[gen_tool],
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="AUTO")),
                temperature=0.7,
                safety_settings=safety_settings
            )
        )

        if not response.candidates:
            await interaction.followup.send("[BLOCKED] 응답이 차단되었습니다.")
            return

        candidate = response.candidates[0]
        text_reply = ""
        generated_prompt = None

        for part in candidate.content.parts:
            if part.text:
                text_reply += part.text
            if part.function_call and part.function_call.name == "generate_image":
                args = part.function_call.args
                generated_prompt = args.get("prompt", prompt)
                ref_count = int(args.get("ref_count", 1 if has_reference else 0))

        if not generated_prompt:
            generated_prompt = prompt

        print(f"  [PROMPT] 정제된 프롬프트: {generated_prompt}")

        if text_reply.strip():
            clean_reply = re.sub(r'\{.*"prompt".*\}', '', text_reply, flags=re.DOTALL).strip()
            if clean_reply:
                if len(clean_reply) > 1900:
                    clean_reply = clean_reply[:1900] + "..."
                await interaction.followup.send(clean_reply)
                print(f"  [BOT]({TEXT_MODEL}): {clean_reply}")

        status_msg = await interaction.followup.send(f"**Generating:** `{generated_prompt}`")

        req_content = ref_images + [generated_prompt] if ref_images else [generated_prompt]
        img_data = await call_nano_banana(req_content)
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

class SoundboardView(discord.ui.View):
    def __init__(self, keys, folder="", search="", user_id=None):
        super().__init__(timeout=None)
        self.folder = folder
        self.search = search
        self.user_id = user_id
        self.page = 0
        self.show_log = False
        self._refresh_keys()
        self._update_components()

    def _refresh_keys(self):
        """bot.sound_files에서 최신 목록 반영."""
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

    def _update_components(self):
        self.clear_items()
        # 폴더 선택 드롭다운
        folders = sorted(set(
            k.split("/")[0] for k in bot.sound_files.keys() if "/" in k
        ))
        if folders:
            folder_options = [discord.SelectOption(label="📂 전체", value="_all", default=self.folder == "")]
            for f in folders[:24]:
                folder_options.append(discord.SelectOption(label=f"📁 {f}", value=f, default=self.folder == f))
            folder_select = discord.ui.Select(
                placeholder="📁 폴더 선택",
                options=folder_options,
                row=0
            )
            folder_select.callback = self._folder_callback
            self.add_item(folder_select)

        # 사운드 재생 드롭다운
        page_keys = self._page_keys()
        if page_keys:
            sound_options = []
            for k in page_keys:
                display = k.split("/")[-1] if "/" in k else k
                sound_options.append(discord.SelectOption(label=display[:100], value=k, description=k[:100] if "/" in k else None))
            sound_select = discord.ui.Select(
                placeholder="🔊 재생할 사운드 선택",
                options=sound_options,
                row=1
            )
            sound_select.callback = self._play_callback
            self.add_item(sound_select)

        # 페이지 버튼
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=2)
            prev_btn.callback = self._prev_callback
            self.add_item(prev_btn)

            page_btn = discord.ui.Button(label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.primary, disabled=True, row=2)
            self.add_item(page_btn)

            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=2)
            next_btn.callback = self._next_callback
            self.add_item(next_btn)

        # 퇴장 버튼
        leave_btn = discord.ui.Button(label="🚪 퇴장", style=discord.ButtonStyle.danger, row=2)
        leave_btn.callback = self._leave_callback
        self.add_item(leave_btn)

        log_label = "📋 목록" if self.show_log else "📋 로그"
        log_btn = discord.ui.Button(label=log_label, style=discord.ButtonStyle.primary if self.show_log else discord.ButtonStyle.secondary, row=3)
        log_btn.callback = self._log_callback
        self.add_item(log_btn)

        channel_btn = discord.ui.Button(label="🔧 이 채널을 업로드용으로", style=discord.ButtonStyle.secondary, row=3)
        channel_btn.callback = self._channel_callback
        self.add_item(channel_btn)

    def _make_embed(self):
        if self.show_log:
            embed = discord.Embed(title="🔊 사운드보드 — 재생 로그", color=0x5865F2)
            if SB_PLAY_LOG:
                lines = [f"`{e['time']}` **{e['user']}** → `{e['sound']}`" for e in reversed(SB_PLAY_LOG)]
                embed.description = "\n".join(lines)
            else:
                embed.description = "(재생 기록 없음)"
            embed.set_footer(text=f"최근 {len(SB_PLAY_LOG)}개")
            return embed

        page_keys = self._page_keys()
        if self.folder:
            title = f"🔊 사운드보드 — {self.folder}/"
        else:
            title = "🔊 사운드보드 — 전체"
        listing = "\n".join(f"`{k}`" for k in page_keys) or "(비어있음)"
        embed = discord.Embed(title=title, description=listing, color=0x5865F2)
        # 마지막 재생 표시
        if SB_PLAY_LOG:
            last = SB_PLAY_LOG[-1]
            embed.set_footer(text=f"총 {len(self.all_keys)}개 | 페이지 {self.page + 1}/{self.total_pages} | 최근: {last['user']} → {last['sound']}")
        else:
            embed.set_footer(text=f"총 {len(self.all_keys)}개 | 페이지 {self.page + 1}/{self.total_pages}")
        return embed

    def _apply_filter(self):
        self.page = 0
        self.total_pages = max(1, (len(self.all_keys) + SOUNDS_PER_PAGE - 1) // SOUNDS_PER_PAGE)
        self._update_components()

    async def _folder_callback(self, interaction: discord.Interaction):
        folder = interaction.data["values"][0]
        self.search = ""
        if folder == "_all":
            self.all_keys = sorted(bot.sound_files.keys())
            self.folder = ""
        else:
            self.all_keys = sorted(k for k in bot.sound_files.keys() if k.startswith(folder + "/"))
            self.folder = folder
        self._apply_filter()
        await interaction.response.edit_message(embed=self._make_embed(), view=self)

    async def _play_callback(self, interaction: discord.Interaction):
        sound_key = interaction.data["values"][0]
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice:
            await temp_reply(interaction, "음성 채널에 먼저 접속해주세요.")
            return
        try:
            success, msg = await play_sound_in_vc(interaction.guild, member, sound_key, bot)
            if success:
                SB_PLAY_LOG.append({
                    "user": interaction.user.display_name,
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
            log_voice_snapshot("수동 퇴장 요청", interaction.guild, vc, extra=f"user={interaction.user.display_name}")
            bot.clear_voice_target(interaction.guild.id)
            await vc.disconnect()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)
        else:
            await temp_reply(interaction, "현재 음성 채널에 접속되어 있지 않습니다.")

    async def _channel_callback(self, interaction: discord.Interaction):
        config = bot.get_config(interaction.guild.id)
        config["sb_channel_id"] = interaction.channel.id
        bot.save_config()
        await temp_reply(interaction,
            f"사운드 업로드 채널: {interaction.channel.mention}\n음원 파일을 올리면 자동으로 추가됩니다.\n봇 재시작 시 이 채널에 /sibo, /sb 임베드가 자동으로 재게시됩니다.")

    async def on_timeout(self):
        self.clear_items()


@bot.tree.command(name="sb", description="사운드보드 (이름 입력시 바로 재생, 빈 입력시 목록)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(name="재생할 사운드 이름 (비우면 목록 표시)")
async def soundboard(interaction: discord.Interaction, name: str = None):
    if not interaction.guild:
        await temp_reply(interaction, "서버에서만 사용 가능합니다.")
        return

    # 인자 없으면 목록 UI 표시
    if not name:
        keys = sorted(bot.sound_files.keys())
        if not keys:
            await temp_reply(interaction, "등록된 사운드가 없습니다.")
            return
        view = SoundboardView(keys, user_id=interaction.user.id)
        bot.active_views.append(view)
        await interaction.response.send_message(embed=view._make_embed(), view=view)
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
                "user": interaction.user.display_name,
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

@bot.tree.command(name="vol", description="사운드보드 볼륨 조절")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(percent="볼륨 (1~100%)")
async def set_volume(interaction: discord.Interaction, percent: int):
    percent = max(1, min(100, percent))
    bot.sound_volume = percent / 100.0
    await temp_reply(interaction, f"사운드보드 볼륨: **{percent}%**")

# ================= 시보 커맨드 (임베드 UI) =================

class SiboView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = str(guild_id)
        self.folder = ""
        self.sound_page = 0
        self._rebuild_sound_keys()
        self._update_components()

    def _get_cfg(self):
        return bot.get_sibo(self.guild_id)

    def _rebuild_sound_keys(self):
        all_keys = sorted(bot.sound_files.keys())
        if self.folder:
            self.sound_keys = [k for k in all_keys if k.startswith(self.folder + "/")]
        else:
            self.sound_keys = all_keys
        self.sound_total_pages = max(1, (len(self.sound_keys) + 24) // 25)
        self.sound_page = min(self.sound_page, self.sound_total_pages - 1)

    def _page_sound_keys(self):
        start = self.sound_page * 25
        return self.sound_keys[start:start + 25]

    def _make_embed(self):
        cfg = self._get_cfg()
        embed = discord.Embed(title="⏰ 시보 설정", color=0xF1C40F)
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
        folder_label = self.folder or "전체"
        embed.set_footer(text=f"음원 폴더: {folder_label} | {len(self.sound_keys)}개 | 페이지 {self.sound_page + 1}/{self.sound_total_pages}")
        return embed

    def _update_components(self):
        self.clear_items()
        cfg = self._get_cfg()

        # 폴더 선택 드롭다운 (row 0)
        folders = sorted(set(k.split("/")[0] for k in bot.sound_files.keys() if "/" in k))
        if folders:
            folder_options = [discord.SelectOption(label="📂 전체", value="_all", default=self.folder == "")]
            for f in folders[:24]:
                folder_options.append(discord.SelectOption(label=f"📁 {f}", value=f, default=self.folder == f))
            folder_select = discord.ui.Select(placeholder="📁 폴더 필터", options=folder_options, row=0)
            folder_select.callback = self._folder_callback
            self.add_item(folder_select)

        # 음원 선택 드롭다운 (row 1)
        page_keys = self._page_sound_keys()
        if page_keys:
            current_sound = cfg.get("sound") if cfg else None
            options = []
            for k in page_keys:
                display = k if len(k) <= 100 else "..." + k[-97:]
                desc = k[:100] if "/" in k else None
                short = k.split("/")[-1] if "/" in k else k
                options.append(discord.SelectOption(label=short[:100], value=k, description=desc, default=k == current_sound))
            sound_select = discord.ui.Select(placeholder="🔊 시보 음원 선택", options=options, row=1)
            sound_select.callback = self._sound_callback
            self.add_item(sound_select)

        # 음원 페이지 버튼 + 설정 버튼 (row 2)
        if self.sound_total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.sound_page == 0, row=2)
            prev_btn.callback = self._sound_prev
            self.add_item(prev_btn)
            page_btn = discord.ui.Button(label=f"{self.sound_page + 1}/{self.sound_total_pages}", style=discord.ButtonStyle.primary, disabled=True, row=2)
            self.add_item(page_btn)
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.sound_page >= self.sound_total_pages - 1, row=2)
            next_btn.callback = self._sound_next
            self.add_item(next_btn)

        # 오프셋 + comp + VC + off 버튼 (row 3)
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

    async def _folder_callback(self, interaction: discord.Interaction):
        folder = interaction.data["values"][0]
        self.folder = "" if folder == "_all" else folder
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
        gid = self.guild_id
        cfg = bot.get_sibo(gid) or {}
        vc_channel = cfg.get("vc_channel")
        offset = cfg.get("offset", 0.0)
        # VC 채널 자동 감지
        if not vc_channel:
            member = interaction.guild.get_member(interaction.user.id)
            if member and member.voice:
                vc_channel = member.voice.channel.id
        bot.set_sibo(gid, {"sound": sound_key, "offset": offset, "vc_channel": vc_channel})
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
        gid = self.guild_id
        cfg = bot.get_sibo(gid)
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

class OffsetModal(discord.ui.Modal, title="오프셋 설정"):
    def __init__(self, sibo_view: SiboView):
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
    def __init__(self, sibo_view: SiboView):
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

@bot.tree.command(name="sibo", description="시보 (정각 알림) 설정")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def sibo_command(interaction: discord.Interaction):
    if not interaction.guild:
        await temp_reply(interaction, "서버에서만 사용 가능합니다.")
        return
    view = SiboView(interaction.guild.id)
    bot.active_views.append(view)
    await interaction.response.send_message(embed=view._make_embed(), view=view)

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
            await temp_reply(interaction,
                f"이 서버 모델: `{server_tm}` (텍스트) / `{server_im}` (이미지)\n글로벌 기본값: `{TEXT_MODEL}` / `{IMAGE_MODEL}`")
        else:
            await temp_reply(interaction,
                f"글로벌 기본 모델: `{TEXT_MODEL}` (텍스트) / `{IMAGE_MODEL}` (이미지)")
        return
    if name not in AVAILABLE_MODELS:
        available = ', '.join(AVAILABLE_MODELS.keys())
        await temp_reply(interaction, f"'{name}' 모델을 찾을 수 없습니다. 사용 가능: {available}")
        return
    TEXT_MODEL = AVAILABLE_MODELS[name]["text"]
    IMAGE_MODEL = AVAILABLE_MODELS[name]["image"]
    if interaction.guild:
        cfg = bot.get_config(interaction.guild_id)
        cfg["text_model"] = TEXT_MODEL
        cfg["image_model"] = IMAGE_MODEL
        bot.save_config()
    await temp_reply(interaction,
        f"모델 변경됨: **{name}**\n텍스트: `{TEXT_MODEL}`\n이미지: `{IMAGE_MODEL}`")

@switch_model.autocomplete('name')
async def model_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(
            name=info['text'],
            value=key
        )
        for key, info in AVAILABLE_MODELS.items()
        if current.lower() in key.lower() or current.lower() in info['text'].lower()
    ][:25]

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
