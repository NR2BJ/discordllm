import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import aiohttp
import asyncio
import base64
import io
import re
import math
from datetime import datetime, timedelta, timezone
from typing import List
from bs4 import BeautifulSoup

ENABLE_UVLOOP = os.environ.get("LLM_ENABLE_UVLOOP", "0") == "1"
if ENABLE_UVLOOP:
    try:
        import uvloop
    except Exception:
        uvloop = None
    else:
        uvloop.install()
else:
    uvloop = None

# ================= 1. 설정 및 상수 =================
DISCORD_TOKEN = os.environ["LLM_DISCORD_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# 🔢 [수치 조절 구역]
HISTORY_LIMIT = 20
IMAGE_HISTORY_LIMIT = 2
VIDEO_HISTORY_LIMIT = 1
AUDIO_HISTORY_LIMIT = 1
MAX_MEDIA_BASE64_MB = 20

# 읽을 수 있는 텍스트 파일 확장자
SUPPORTED_EXTENSIONS = ('.txt', '.py', '.c', '.cpp', '.java', '.js', '.html', 
                        '.css', '.md', '.json', '.xml', '.yaml', '.sh', '.rb', '.go', '.rs')

DEFAULT_MODEL = "google/gemini-3-flash-preview"
VOICE_RECONNECT_DELAY = 3.0
VOICE_RECONNECT_COOLDOWN = 20.0
VOICE_HEALTH_GRACE = 30.0
VOICE_HEALTH_BAD_LIMIT = 3
VOICE_HEALTH_RECOVERY = os.environ.get("LLM_VOICE_HEALTH_RECOVERY", "0") == "1"
VOICE_SELF_DEAF = True
VOICE_SELF_MUTE = False
VOICE_FORCE_STATE = os.environ.get("LLM_VOICE_FORCE_STATE", "0") == "1"
DOC_CACHE_AUTO_REPLAY_WINDOW = 2

_COMMON_RULES = """## General Rules
- Output response content directly. No prefixes like 'Bot:' or 'Model:'.
- Respond in the user's language and mirror their tone (casual, formal, vulgar — match them).
- If the user sets up a specific roleplay scenario (character, world, setting), maintain it consistently within that conversation. Do NOT treat casual chat as "established context" that must persist.
- If you see `[System Error]` or `[Error]` in history, YOUR previous action failed.
- When a URL is in the message, the system fetches it and attaches `[Webpage Content]` — use that content directly.
- Analyze attached files (images/video/audio) when supported.
- Keep responses concise and Discord-appropriate.
- If the model name ends with `:online`, actively use web search for up-to-date info. Otherwise rely on your training knowledge."""

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
"""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
KST = timezone(timedelta(hours=9))
DOC_CACHE_DIR = os.environ.get("LLM_DOC_CACHE_DIR", os.path.join(os.path.dirname(CONFIG_FILE), "llm_doc_cache"))
VOICE_DEBUG_LOGS = False
HISTORY_DEBUG_LOGS = False

# ================= 2. 데이터 관리자 =================
class DataManager:
    @staticmethod
    def load_json(filename, default_data):
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return default_data
        return default_data

    @staticmethod
    def save_json(filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except:
            pass

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

def get_voice_unhealthy_reason(vc):
    if vc is None:
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
    try:
        connected = vc.is_connected()
    except Exception as e:
        connected = f"err:{e.__class__.__name__}"
    try:
        playing = vc.is_playing()
    except Exception as e:
        playing = f"err:{e.__class__.__name__}"
    endpoint = getattr(vc, "endpoint", None) or "-"
    session_id = getattr(vc, "session_id", None) or "-"
    ws_closed = get_voice_ws_closed(vc)
    return (
        f"connected={connected} "
        f"playing={playing} "
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
        f"self_deaf={getattr(state, 'self_deaf', None)}"
    )

def log_voice_snapshot(prefix, guild=None, vc=None, extra=None):
    if not VOICE_DEBUG_LOGS:
        return
    parts = [f"[VC][LLM] {prefix}"]
    if guild is not None:
        parts.append(f"guild={guild.name}({guild.id})")
    if extra:
        parts.append(extra)
    parts.append(describe_voice_client(vc))
    print(" | ".join(parts))

def log_voice_debug(message):
    if VOICE_DEBUG_LOGS:
        print(message)

def log_history_debug(message):
    if HISTORY_DEBUG_LOGS:
        print(message)

async def cleanup_stale_voice_client(vc, guild):
    log_voice_snapshot("stale VC 정리 시작", guild, vc)
    try:
        await vc.disconnect(force=True)
    except TypeError:
        try:
            await vc.disconnect()
        except Exception as e:
            log_voice_debug(f"[VC][LLM] stale disconnect fallback 실패 ({guild.name}): {e}")
    except Exception as e:
        log_voice_debug(f"[VC][LLM] stale disconnect 실패 ({guild.name}): {e}")
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
            log_voice_debug(f"[VC][LLM] 채널 조회 실패 ({guild.name}, {channel_id}): {e}")
            return None
    if not hasattr(channel, "connect"):
        log_voice_debug(f"[VC][LLM] 연결 불가 채널 타입 ({guild.name}, {channel_id})")
        return None
    return channel

async def apply_voice_preferences(guild, channel):
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
        log_voice_debug(f"[VC][LLM] voice state 선호값 적용 실패 ({guild.name}): {e}")

async def ensure_voice_connection(guild, channel_id, *, move=True, reason="unknown"):
    channel = await resolve_voice_channel(guild, channel_id)
    if channel is None:
        return None

    async with bot.get_voice_lock(guild.id):
        vc = guild.voice_client
        log_voice_snapshot(
            f"ensure_voice_connection [{reason}]",
            guild,
            vc,
            extra=f"target={format_channel_ref(channel)} move={move}",
        )
        if vc and vc.is_connected():
            if move and vc.channel != channel:
                await vc.move_to(channel)
                log_voice_snapshot(
                    f"이동 완료 [{reason}]",
                    guild,
                    vc,
                    extra=f"target={format_channel_ref(channel)}",
                )
            await apply_voice_preferences(guild, channel)
            bot.mark_voice_connected(guild.id)
            return vc

        if vc:
            await cleanup_stale_voice_client(vc, guild)

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
        return vc

# ================= 3. OpenRouter API 클라이언트 =================
class OpenRouterClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://discord.com",
            "X-Title": "Discord Bot",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (OpenRouterClient, 1.0)"
        }
        self.models_list = []

    async def fetch_models_metadata(self):
        try:
            print("⏳ OpenRouter 모델 리스트 동기화 중...")
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/models", headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_list = data.get('data', [])
                        raw_list.sort(key=lambda x: x.get('created') or 0, reverse=True)
                        self.models_list = raw_list
                        print(f"📚 {len(self.models_list)}개 모델 로드 완료")
                    else:
                        print(f"⚠️ 모델 정보 로드 실패: {resp.status}")
        except Exception as e:
            print(f"⚠️ 모델 정보 로드 중 오류: {e}")

    def get_model_info(self, model_id):
        clean_id = model_id.split(":")[0]
        for m in self.models_list:
            if m['id'] == clean_id: return m
        return None

    def analyze_model_capabilities(self, model_id):
        """모델의 입출력 capability 반환: {"input": [...], "output": [...]}"""
        clean_id = model_id.split(":")[0]
        model = self.get_model_info(clean_id)
        if not model:
            return {"input": ["text"], "output": ["text"]}

        input_mod = model.get("input_modalities")
        output_mod = model.get("output_modalities")

        if input_mod and output_mod:
            return {"input": input_mod, "output": output_mod}

        # fallback: architecture.modality 파싱 ("text+image->text" 등)
        arch = model.get('architecture', {})
        modality = arch.get('modality', 'text->text').lower()
        parts = modality.split('->')
        inp = parts[0] if parts else 'text'
        out = parts[1] if len(parts) > 1 else 'text'
        input_list = [m.strip() for m in inp.split('+')]
        output_list = [m.strip() for m in out.split('+')]
        return {"input": input_list, "output": output_list}

    def analyze_model_type(self, model_id):
        caps = self.analyze_model_capabilities(model_id)
        if "image" in caps["input"]:
            return 'multimodal'
        return 'text'

    def get_type_string(self, model_id):
        caps = self.analyze_model_capabilities(model_id)
        parts = []
        if "image" in caps["input"]: parts.append("Vision")
        if "video" in caps["input"]: parts.append("Video")
        if "audio" in caps["input"]: parts.append("Audio")
        if "image" in caps["output"]: parts.append("Image Gen")
        if "audio" in caps["output"]: parts.append("Audio Gen")
        if parts:
            return "Multimodal (" + ", ".join(parts) + ")"
        return "Text Generation"

    async def chat_completion(self, model, messages, capabilities=None):
        import ast  # 문자열 형태의 리스트 파싱을 위해 추가

        payload = {
            "model": model,
            "messages": messages,
            "transforms": ["middle-out"],
            # "temperature": 0.7
        }

        # 이미지 생성 가능 모델이면 modalities 추가
        if capabilities and "image" in capabilities.get("output", []):
            payload["modalities"] = ["text", "image"]

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", headers=self.headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choice = data['choices'][0]
                    usage = data.get('usage', {})

                    raw_content = choice['message'].get('content', '')

                    # 디버그: raw 응답 구조 로그
                    msg_keys = list(choice['message'].keys())
                    data_keys = [k for k in data.keys() if k not in ('id', 'object', 'created', 'model', 'choices', 'usage')]
                    print(f"[DEBUG] message keys: {msg_keys}, content type: {type(raw_content).__name__}, extra data keys: {data_keys}")
                    if raw_content is None:
                        print(f"[DEBUG] content is None, full message: {json.dumps({k: str(v)[:200] for k, v in choice['message'].items()}, ensure_ascii=False)}")
                    final_content = ""
                    images = []

                    if raw_content is None:
                        raw_content = ''

                    # 이미지 추출: choice.message 내 별도 필드
                    def _extract_images(source, keys):
                        """source dict에서 keys에 해당하는 이미지 데이터 추출"""
                        found = []
                        for key in keys:
                            extra_imgs = source.get(key, [])
                            if isinstance(extra_imgs, list):
                                for item in extra_imgs:
                                    if isinstance(item, str):
                                        found.append(item)
                                    elif isinstance(item, dict):
                                        if item.get('type') == 'image_url':
                                            img_url = item.get('image_url', {}).get('url', '')
                                            if img_url:
                                                found.append(img_url)
                                        else:
                                            url = item.get('url') or item.get('b64_json') or item.get('data', '')
                                            if url:
                                                found.append(url)
                            elif isinstance(extra_imgs, str) and extra_imgs:
                                found.append(extra_imgs)
                        return found

                    # choice.message 내 이미지
                    images.extend(_extract_images(choice['message'], ['images', 'image']))
                    # 응답 최상위 data 필드 (DALL-E 스타일)
                    images.extend(_extract_images(data, ['data', 'images']))

                    # 1. 진짜 리스트인 경우 (일반적인 경우)
                    if isinstance(raw_content, list):
                        for part in raw_content:
                            if isinstance(part, dict):
                                if part.get('type') == 'text':
                                    final_content += part.get('text', '')
                                elif part.get('type') == 'image_url':
                                    img_url = part.get('image_url', {}).get('url', '')
                                    if img_url:
                                        images.append(img_url)
                                elif part.get('type') == 'image':
                                    # Anthropic-style: {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
                                    src = part.get('source', {})
                                    if src.get('data'):
                                        mt = src.get('media_type', 'image/png')
                                        images.append(f"data:{mt};base64,{src['data']}")

                    # 2. 문자열인데 리스트처럼 생긴 경우 ("[{'type':...}]") -> DeepSeek 이슈 해결용
                    elif isinstance(raw_content, str):
                        stripped = raw_content.strip()
                        if stripped.startswith('[') and stripped.endswith(']'):
                            try:
                                # 문자열을 실제 리스트 객체로 변환 시도
                                parsed_list = ast.literal_eval(stripped)
                                if isinstance(parsed_list, list):
                                    for part in parsed_list:
                                        if isinstance(part, dict):
                                            if part.get('type') == 'text':
                                                final_content += part.get('text', '')
                                            elif part.get('type') == 'image_url':
                                                img_url = part.get('image_url', {}).get('url', '')
                                                if img_url:
                                                    images.append(img_url)
                                    # 파싱에 성공했지만 텍스트가 없으면 원본 사용
                                    if not final_content and not images:
                                        final_content = raw_content
                                else:
                                    final_content = raw_content
                            except:
                                # 파싱 실패 시 그냥 원본 문자열 사용
                                final_content = raw_content
                        else:
                            final_content = raw_content

                    else:
                        final_content = str(raw_content)

                    # 빈 값 처리
                    if not final_content and not images:
                        final_content = "..."

                    return final_content, usage, images
                else:
                    error_text = await resp.text()
                    raise Exception(f"API Error ({resp.status}): {error_text}")

# ================= 4. 봇 클래스 =================
class OpenRouterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.or_client = OpenRouterClient(OPENROUTER_API_KEY)
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}
        self.voice_locks = {}
        self.voice_retry_after = {}
        self.voice_connected_at = {}
        self.voice_bad_state_counts = {}
        self.doc_cache = DocumentCache(DOC_CACHE_DIR)

    async def setup_hook(self):
        await self.or_client.fetch_models_metadata()
        await self.tree.sync()
        print("✅ OpenRouter Bot 준비 완료")

    def get_config(self, guild_id):
        gid = str(guild_id)
        if gid not in self.server_configs:
            self.server_configs[gid] = {
                "channel_id": None,
                "model": DEFAULT_MODEL,
                "reset_time": None,
                "search": False
            }
        return self.server_configs[gid]

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
        return self.get_config(guild_id).get("voice_target_channel_id")

    def set_voice_target(self, guild_id, channel_id):
        cfg = self.get_config(guild_id)
        if cfg.get("voice_target_channel_id") != channel_id:
            cfg["voice_target_channel_id"] = channel_id
            self.save_config()

    def clear_voice_target(self, guild_id):
        cfg = self.get_config(guild_id)
        if "voice_target_channel_id" in cfg:
            cfg.pop("voice_target_channel_id", None)
            self.save_config()

    def can_retry_voice(self, guild_id):
        now = asyncio.get_running_loop().time()
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
        self.voice_connected_at[gid] = asyncio.get_running_loop().time()
        self.voice_bad_state_counts[gid] = 0

    def mark_voice_disconnected(self, guild_id):
        gid = str(guild_id)
        self.voice_connected_at.pop(gid, None)
        self.voice_bad_state_counts[gid] = 0

    def get_voice_connected_age(self, guild_id):
        started = self.voice_connected_at.get(str(guild_id))
        if started is None:
            return None
        return asyncio.get_running_loop().time() - started

    def record_voice_health(self, guild_id, unhealthy_reason):
        gid = str(guild_id)
        if unhealthy_reason:
            self.voice_bad_state_counts[gid] = self.voice_bad_state_counts.get(gid, 0) + 1
            return self.voice_bad_state_counts[gid]
        self.voice_bad_state_counts[gid] = 0
        return 0

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)

bot = OpenRouterBot()

@tasks.loop(seconds=10)
async def voice_watchdog():
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
                        await cleanup_stale_voice_client(vc, guild)
                        vc = await ensure_voice_connection(guild, target_channel_id, reason="watchdog_unhealthy")
                        if vc:
                            bot.reset_voice_retry(guild.id)
                        continue
                else:
                    bot.record_voice_health(guild.id, None)
            except Exception as e:
                log_voice_debug(f"[VC][LLM] watchdog 상태점검 실패 ({guild.name}): {e}")
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
            vc = await ensure_voice_connection(guild, target_channel_id, reason="watchdog")
            if vc:
                bot.reset_voice_retry(guild.id)
        except Exception as e:
            log_voice_debug(f"[VC][LLM] watchdog 재접속 실패 ({guild.name}): {e}")

# ================= 5. 유틸리티 함수 =================
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

async def encode_image(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode('utf-8'), resp.headers.get('Content-Type')
    except: pass
    return None, None

async def send_long_message(channel, content):
    if len(content) <= 2000:
        await channel.send(content)
        return
    
    chunks = []
    while content:
        if len(content) <= 1900:
            chunks.append(content)
            break
        split_index = content.rfind('\n', 0, 1900)
        if split_index == -1: split_index = 1900
        chunks.append(content[:split_index])
        content = content[split_index:].lstrip()

    for chunk in chunks:
        await channel.send(chunk)
        await asyncio.sleep(0.5)

def format_tokens(n):
    if n is None: return "N/A"
    try:
        n = int(n)
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000: return f"{n/1_000:.0f}K"
        return str(n)
    except: return "N/A"

def format_date(timestamp):
    if not timestamp: return "N/A"
    try:
        dt = datetime.fromtimestamp(int(timestamp))
        return dt.strftime("%Y-%m-%d")
    except: return "N/A"

# ================= 6. 이벤트 핸들러 =================
@tasks.loop(hours=1)
async def refresh_models_hourly():
    """매 시간 OpenRouter 모델 리스트 자동 갱신."""
    print("[HOURLY] 모델 리스트 자동 갱신 시작")
    await bot.or_client.fetch_models_metadata()

@refresh_models_hourly.before_loop
async def _before_refresh_models_hourly():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"🤖 로그인: {bot.user}")
    if not refresh_models_hourly.is_running():
        refresh_models_hourly.start()
        print("[HOURLY] 모델 자동 갱신 스케줄러 시작 (매 1시간)")
    if not voice_watchdog.is_running():
        voice_watchdog.start()
    for guild in bot.guilds:
        target_channel_id = bot.get_voice_target(guild.id)
        if guild.voice_client and guild.voice_client.is_connected():
            bot.mark_voice_connected(guild.id)
        if target_channel_id:
            log_voice_snapshot(
                "on_ready keepalive 상태",
                guild,
                guild.voice_client,
                extra=f"target_channel_id={target_channel_id}",
            )

@bot.event
async def on_voice_state_update(member, before, after):
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
        return

    target_channel_id = bot.get_voice_target(member.guild.id)
    if not target_channel_id:
        return

    bot.mark_voice_disconnected(member.guild.id)
    await asyncio.sleep(VOICE_RECONNECT_DELAY)
    try:
        vc = await ensure_voice_connection(member.guild, target_channel_id, reason="voice_state_update")
        if vc:
            bot.reset_voice_retry(member.guild.id)
    except Exception as e:
        log_voice_debug(f"[VC][LLM] 이벤트 기반 재접속 실패 ({member.guild.name}): {e}")

@bot.event
async def on_message(message):
    if message.author.bot: return
    if not message.guild: return
    config = bot.get_config(message.guild.id)
    if message.channel.id != config.get("channel_id"): return

    lock = bot.get_channel_lock(message.channel.id)
    async with lock:
        async with message.channel.typing():
            try:
                base_model = config["model"]
                current_model = base_model + ":online" if config.get("search") else base_model
                caps = bot.or_client.analyze_model_capabilities(base_model)
                can_see_image = "image" in caps["input"]
                can_see_video = "video" in caps["input"]
                can_see_audio = "audio" in caps["input"]
                can_gen_image = "image" in caps["output"]
                print(f"[CAPS] {base_model}: in={caps['input']} out={caps['output']}")

                # 동적 시스템 프롬프트
                sys_prompt = SYSTEM_PROMPT
                capability_hints = []
                if can_see_image: capability_hints.append("You can analyze images attached by users.")
                if can_see_video: capability_hints.append("You can analyze video files attached by users.")
                if can_see_audio: capability_hints.append("You can analyze audio files attached by users.")
                if can_gen_image: capability_hints.append("You can generate images when the user requests it.")

                cannot_hints = []
                if not can_see_image: cannot_hints.append("analyze images")
                if not can_see_video: cannot_hints.append("analyze videos")
                if not can_see_audio: cannot_hints.append("analyze audio")
                if not can_gen_image: cannot_hints.append("generate images")
                if cannot_hints:
                    capability_hints.append(f"You CANNOT {', '.join(cannot_hints)}. If asked, politely tell the user to switch to a model that supports it using /model command.")

                if capability_hints:
                    sys_prompt += "\n" + "\n".join(f"- {h}" for h in capability_hints)

                messages = []
                messages.append({"role": "system", "content": sys_prompt})

                reset_dt = None
                if config.get("reset_time"):
                    reset_dt = datetime.fromisoformat(config["reset_time"])
                reply_target_id = message.reference.message_id if message.reference and message.reference.message_id else None

                # 히스토리 구축
                img_count = 0
                vid_count = 0
                aud_count = 0
                history_buffer = []
                history_position = 0
                async for msg in message.channel.history(limit=HISTORY_LIMIT + 10):
                    if msg.id == message.id: continue
                    if msg.id > message.id and msg.author != bot.user: continue
                    if reset_dt and msg.created_at < reset_dt: break

                    if msg.author == bot.user and msg.interaction_metadata is not None:
                        continue

                    is_bot = (msg.author == bot.user)
                    role = "assistant" if is_bot else "user"
                    content_list = []
                    cached_doc_blocks = []
                    if not is_bot:
                        replay_full_docs = (reply_target_id == msg.id) or (history_position < DOC_CACHE_AUTO_REPLAY_WINDOW)
                        cached_doc_blocks = bot.doc_cache.render_blocks(msg.id, full_content=replay_full_docs)

                    text_content = ""
                    if is_bot:
                        clean_content = msg.content
                        if ":" in clean_content[:20]:
                            parts = clean_content.split(": ", 1)
                            if len(parts) > 1 and parts[0] in [bot.user.display_name, bot.user.name, "Bot", "Model"]:
                                clean_content = parts[1]
                        text_content = clean_content
                        if msg.attachments:
                            for att in msg.attachments:
                                ct = att.content_type or ""
                                file_size = att.size or 0
                                too_large = file_size > MAX_MEDIA_BASE64_MB * 1024 * 1024
                                if ct.startswith('image') and can_see_image and img_count < IMAGE_HISTORY_LIMIT and not too_large:
                                    b64, mime = await encode_image(att.url)
                                    if b64:
                                        content_list.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                                        img_count += 1
                                        log_history_debug(f"[HIST] 봇 이미지 히스토리 포함: {att.filename} ({len(b64)} chars)")
                                    else:
                                        log_history_debug(f"[HIST] 봇 이미지 인코딩 실패: {att.url[:80]}")
                                        # 마커 없이 스킵
                                elif ct.startswith('image'):
                                    reason = "no_cap" if not can_see_image else f"limit({img_count}>={IMAGE_HISTORY_LIMIT})" if img_count >= IMAGE_HISTORY_LIMIT else f"too_large({file_size})"
                                    log_history_debug(f"[HIST] 봇 이미지 스킵: {reason}")
                                    # 마커 없이 스킵
                                elif ct.startswith('video'):
                                    text_content += "\n(Bot generated a video here)"
                    else:
                        _member = message.guild.get_member(msg.author.id) or msg.author
                        text_content = f"[사용자:{_member.display_name}|ID:{msg.author.id}]: {msg.content}"

                        if msg.attachments:
                            for att in msg.attachments:
                                ct = att.content_type or ""
                                file_size = att.size or 0
                                too_large = file_size > MAX_MEDIA_BASE64_MB * 1024 * 1024

                                if ct.startswith('image') and can_see_image and img_count < IMAGE_HISTORY_LIMIT and not too_large:
                                    b64, mime = await encode_image(att.url)
                                    if b64:
                                        content_list.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                                        img_count += 1
                                    else:
                                        text_content += f"\n(User attached an image: {att.filename})"
                                elif ct.startswith('video') and can_see_video and vid_count < VIDEO_HISTORY_LIMIT and not too_large:
                                    b64, mime = await encode_image(att.url)
                                    if b64:
                                        content_list.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
                                        vid_count += 1
                                    else:
                                        text_content += f"\n(User attached a video: {att.filename})"
                                elif ct.startswith('audio') and can_see_audio and aud_count < AUDIO_HISTORY_LIMIT and not too_large:
                                    b64, _ = await encode_image(att.url)
                                    if b64:
                                        fmt = att.filename.rsplit('.', 1)[-1].lower() if '.' in att.filename else 'mp3'
                                        content_list.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
                                        aud_count += 1
                                    else:
                                        text_content += f"\n(User attached an audio file: {att.filename})"
                                elif att.filename.endswith(SUPPORTED_EXTENSIONS) and not bot.doc_cache.has_source(msg.id, "text_file", att.filename):
                                    try:
                                        raw_bytes = await att.read()
                                        for enc in ['utf-8', 'cp949', 'latin1']:
                                            try:
                                                decoded = raw_bytes.decode(enc)
                                                text_content += f"\n[File: {att.filename}]\n{decoded}"
                                                break
                                            except: continue
                                    except: pass
                                else:
                                    if ct.startswith('image'): text_content += f"\n(User attached an image: {att.filename})"
                                    elif ct.startswith('video'): text_content += f"\n(User attached a video: {att.filename})"
                                    elif ct.startswith('audio'): text_content += f"\n(User attached an audio file: {att.filename})"

                    if cached_doc_blocks:
                        text_content += "\n\n" + "\n\n".join(cached_doc_blocks)

                    # 봇 메시지인데 텍스트도 없고 이미지도 없으면 히스토리 제외
                    if is_bot and not text_content.strip() and len(content_list) == 0:
                        continue
                    content_list.append({"type": "text", "text": text_content})
                    history_buffer.insert(0, {"role": role, "content": content_list})
                    history_position += 1

                messages.extend(history_buffer)

                # 현재 메시지 처리
                current_content = []
                _cur_member = message.guild.get_member(message.author.id) or message.author
                user_text = f"[사용자:{_cur_member.display_name}|ID:{message.author.id}]: {message.content}"

                # URL 웹 스크래핑
                urls = re.findall(r'(https?://\S+)', message.content)
                for url in urls:
                    web_content = await scrap_webpage(url)
                    if web_content:
                        doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
                        print(f"[CACHE] webpage 저장: {doc_id} ({url})")
                        user_text += f"\n\n[Webpage Content: {url}]\n```\n{web_content}\n```"
                        print(f"[WEB] 웹사이트 내용 읽음: {url}")

                has_media = False
                if message.attachments:
                    for att in message.attachments:
                        ct = att.content_type or ""
                        file_size = att.size or 0
                        too_large = file_size > MAX_MEDIA_BASE64_MB * 1024 * 1024
                        print(f"[ATT] {att.filename} ct={ct} size={file_size} can_see_image={can_see_image} too_large={too_large}")

                        if ct.startswith('image') and can_see_image and not too_large:
                            b64, mime = await encode_image(att.url)
                            if b64:
                                current_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                                print(f"[ATT] 이미지 base64 인코딩 완료: {att.filename} ({len(b64)} chars)")
                                has_media = True
                        elif ct.startswith('video') and can_see_video and not too_large:
                            b64, mime = await encode_image(att.url)
                            if b64:
                                current_content.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
                                has_media = True
                        elif ct.startswith('audio') and can_see_audio and not too_large:
                            b64, _ = await encode_image(att.url)
                            if b64:
                                fmt = att.filename.rsplit('.', 1)[-1].lower() if '.' in att.filename else 'mp3'
                                current_content.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
                                has_media = True
                        elif att.filename.endswith(SUPPORTED_EXTENSIONS):
                             try:
                                raw_bytes = await att.read()
                                for enc in ['utf-8', 'cp949', 'latin1']:
                                    try:
                                        decoded = raw_bytes.decode(enc)
                                        doc_id = bot.doc_cache.store_document("text_file", att.filename, decoded, message.id, message.channel.id)
                                        print(f"[CACHE] text file 저장: {doc_id} ({att.filename})")
                                        user_text += f"\n[Current File: {att.filename}]\n{decoded}"
                                        break
                                    except: continue
                             except: pass

                        if too_large:
                            media_type = ct.split('/')[0] if '/' in ct else 'file'
                            user_text += f"\n(User attached a {media_type} file: {att.filename}, but it's too large to process: {file_size // (1024*1024)}MB)"

                current_content.append({"type": "text", "text": user_text})
                messages.append({"role": "user", "content": current_content})

                # API 호출
                try:
                    response, usage, images = await bot.or_client.chat_completion(current_model, messages, capabilities=caps)
                except Exception as e:
                    err_msg = str(e).lower()
                    # 이미지 관련 400: 현재 메시지 미디어 없어도 히스토리 이미지가 원인일 수 있음
                    has_any_media = has_media or any(
                        isinstance(m.get('content'), list) and
                        any(p.get('type') in ('image_url', 'video_url', 'input_audio') for p in m['content'])
                        for m in messages
                    )
                    is_media_error = "400" in err_msg or "404" in err_msg or "endpoint" in err_msg or "'choices'" in err_msg
                    if is_media_error and has_any_media:
                        print(f"⚠️ [Media Error] {current_model}: 텍스트 모드로 전환.")
                        await message.channel.send(f"⚠️ `{current_model}` 모델이 미디어를 처리하지 못했습니다. 텍스트 모드로 재시도합니다.")
                        for m in messages:
                            if isinstance(m['content'], list):
                                m['content'] = [c for c in m['content'] if c.get('type') == 'text']
                        if has_media:
                            last_msg = messages[-1]
                            if isinstance(last_msg['content'], list):
                                for part in last_msg['content']:
                                    if part.get('type') == 'text':
                                        part['text'] += "\n(System: 미디어 첨부가 있었으나 이 모델이 처리하지 못했습니다.)"
                                        break
                        response, usage, images = await bot.or_client.chat_completion(current_model, messages)
                    else:
                        raise e

                if response.startswith(f"{bot.user.display_name}:"):
                    response = response.replace(f"{bot.user.display_name}:", "", 1).strip()

                total_tokens = usage.get('total_tokens', 'N/A')
                log_time = datetime.now().strftime('%H:%M:%S')

                print(f"[{log_time}] [{message.guild.name}]{message.author.display_name}: {message.content}")
                print(f"봇({current_model}): {response} (Tokens: {total_tokens})")
                if images:
                    print(f"  [이미지 생성]: {len(images)}장")
                print("-------")

                if response:
                    await send_long_message(message.channel, response)

                # 생성된 이미지 전송
                for idx, img_data_url in enumerate(images):
                    try:
                        if img_data_url.startswith("data:"):
                            header, b64_data = img_data_url.split(",", 1)
                            # header: "data:image/png;base64"
                            mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                            ext = mime.split("/")[-1] if "/" in mime else "png"
                        else:
                            b64_data = img_data_url
                            ext = "png"
                        binary = base64.b64decode(b64_data)
                        await message.channel.send(file=discord.File(fp=io.BytesIO(binary), filename=f"generated_{idx}.{ext}"))
                    except Exception as img_err:
                        print(f"⚠️ 이미지 전송 실패: {img_err}")

            except Exception as e:
                print(f"❌ 오류: {str(e)}")
                await message.channel.send(f"❌ 오류: {str(e)}")

# ================= 7. 명령어 =================

async def model_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if not bot.or_client.models_list:
        await bot.or_client.fetch_models_metadata()
    
    models = bot.or_client.models_list
    current_lower = current.lower()
    
    filtered = [m for m in models if current_lower in m['id'].lower() or current_lower in m['name'].lower()]
    
    choices = []
    for m in filtered[:25]:
        name_display = f"{m['name']} ({format_date(m.get('created'))})"
        if len(name_display) > 100: name_display = name_display[:100]
        choices.append(app_commands.Choice(name=name_display, value=m['id']))
    return choices

@bot.tree.command(name="model", description="사용할 AI 모델 변경")
@app_commands.autocomplete(model_id=model_autocomplete)
@app_commands.describe(model_id="모델 이름 검색")
async def set_model(interaction: discord.Interaction, model_id: str):
    clean_id = model_id.split(":")[0]
    model_info = bot.or_client.get_model_info(clean_id)
    
    if not model_info:
        config = bot.get_config(interaction.guild_id)
        config["model"] = clean_id
        bot.save_config()
        await interaction.response.send_message(f"✅ 모델 변경 완료: `{clean_id}` (정보 없음)")
        return

    config = bot.get_config(interaction.guild_id)
    config["model"] = model_info['id']
    bot.save_config()
    
    type_str = bot.or_client.get_type_string(model_info['id'])
    created = format_date(model_info.get('created'))

    await interaction.response.send_message(f"✅ 모델 변경 완료!\n**{model_info['name']}** (`{model_info['id']}`)\n타입: **{type_str}** | 출시일: {created}")

@bot.tree.command(name="dashboard", description="현재 모델 스펙 확인")
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()
    config = bot.get_config(interaction.guild_id)
    curr_model = config['model']
    
    if not bot.or_client.models_list:
        await bot.or_client.fetch_models_metadata()
        
    model_meta = bot.or_client.get_model_info(curr_model) or {}
    type_str = bot.or_client.get_type_string(curr_model)
    caps = bot.or_client.analyze_model_capabilities(curr_model)
    is_multimodal = len(caps["input"]) > 1 or len(caps["output"]) > 1

    color = 0x00bcd4 if is_multimodal else 0x4caf50
    embed = discord.Embed(title="🎛️ OpenRouter Dashboard", color=color)

    pricing = model_meta.get('pricing', {})
    try:
        in_p = float(pricing.get('prompt', 0)) * 1_000_000
        out_p = float(pricing.get('completion', 0)) * 1_000_000
        def _fmt_price(v):
            if v == 0: return "Free"
            if v < 0.001: return f"${v:.6f}"
            if v < 0.01: return f"${v:.4f}"
            if v < 1: return f"${v:.3f}"
            return f"${v:.2f}"
        price_str = f"In: {_fmt_price(in_p)} / Out: {_fmt_price(out_p)}"
    except: price_str = "N/A"

    context = format_tokens(model_meta.get('context_length'))
    max_out = format_tokens(model_meta.get('top_provider', {}).get('max_completion_tokens'))
    created = format_date(model_meta.get('created'))

    embed.add_field(name=f"🧠 {model_meta.get('name', curr_model)}", value=f"ID: `{curr_model}`\nType: **{type_str}**\nReleased: **{created}**", inline=False)
    embed.add_field(name="⚙️ Specs", value=f"• **Context:** {context}\n• **Max Out:** {max_out}\n• **Price(1M):** {price_str}", inline=True)

    # Modality 표시
    input_str = ", ".join(caps["input"]) if caps["input"] else "text"
    output_str = ", ".join(caps["output"]) if caps["output"] else "text"
    embed.add_field(name="🔄 Modalities", value=f"• **Input:** {input_str}\n• **Output:** {output_str}", inline=True)

    desc = model_meta.get('description', '설명 없음')
    if len(desc) > 300: desc = desc[:297] + "..."
    embed.add_field(name="📝 Description", value=desc, inline=False)
    embed.set_footer(text=f"Time: {datetime.now(KST).strftime('%H:%M:%S')}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="set", description="응답 채널 설정")
async def set_channel(interaction: discord.Interaction):
    config = bot.get_config(interaction.guild_id)
    config["channel_id"] = interaction.channel_id
    bot.save_config()
    await interaction.response.send_message(f"✅ 활성 채널: {interaction.channel.name}")

@bot.tree.command(name="reset", description="대화 초기화")
async def reset(interaction: discord.Interaction):
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    bot.save_config()
    await interaction.response.send_message("🧹 대화 맥락 초기화 완료")

@bot.tree.command(name="search", description="웹 검색 기능 ON/OFF")
async def toggle_search(interaction: discord.Interaction):
    config = bot.get_config(interaction.guild_id)
    config["search"] = not config.get("search", False)
    bot.save_config()
    state = "ON 🔍" if config["search"] else "OFF"
    model_display = config["model"] + (":online" if config["search"] else "")
    await interaction.response.send_message(f"✅ 웹 검색: **{state}**\n모델: `{model_display}`")

@bot.tree.command(name="refresh_models", description="OpenRouter 모델 리스트 즉시 갱신")
async def refresh_models_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    before = len(bot.or_client.models_list)
    await bot.or_client.fetch_models_metadata()
    after = len(bot.or_client.models_list)
    if after == 0:
        await interaction.followup.send("[ERROR] 모델 리스트 갱신 실패 — OpenRouter API 응답 확인 필요", ephemeral=True)
    elif after == before:
        await interaction.followup.send(f"✅ 갱신 완료 (변동 없음, {after}개 모델)", ephemeral=True)
    else:
        delta = after - before
        sign = "+" if delta > 0 else ""
        await interaction.followup.send(f"✅ 갱신 완료: {before}개 → **{after}개** ({sign}{delta})", ephemeral=True)

@bot.tree.command(name="vcjoin", description="현재 들어가 있는 음성 채널에 접속")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def vc_join(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if not interaction.guild or not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("[ERROR] 음성 채널에 먼저 접속해주세요.")
        return

    bot.set_voice_target(interaction.guild.id, member.voice.channel.id)
    vc = await ensure_voice_connection(interaction.guild, member.voice.channel.id, reason="command")
    if vc is None:
        await interaction.response.send_message("[ERROR] 음성 채널 연결에 실패했습니다.")
        return

    await interaction.response.send_message(f"[OK] LLM VC 접속: {member.voice.channel.mention}")

@bot.tree.command(name="vcleave", description="음성 채널에서 나가기")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def vc_leave(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.")
        return

    vc = interaction.guild.voice_client
    bot.clear_voice_target(interaction.guild.id)
    if vc and vc.is_connected():
        await vc.disconnect()
        await interaction.response.send_message("[OK] LLM VC 퇴장")
    else:
        await interaction.response.send_message("[OK] 현재 음성 채널에 접속되어 있지 않습니다.")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
