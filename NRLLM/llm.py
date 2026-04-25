import discord
from discord import app_commands
from discord.ext import commands, tasks
import csv
import json
import os
import aiohttp
import asyncio
import base64
import copy
import io
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from bs4 import BeautifulSoup

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


def is_enabled(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)

# 🔢 [수치 조절 구역]
HISTORY_LIMIT = 20
MIN_HISTORY_LIMIT = 1
MAX_HISTORY_LIMIT = 100
IMAGE_HISTORY_LIMIT = 2
VIDEO_HISTORY_LIMIT = 1
AUDIO_HISTORY_LIMIT = 1
MAX_MEDIA_BASE64_MB = 20
MAX_DOC_CHARS = 12000
MEMORY_EXCERPT_MAX_CHARS = 5000

# 읽을 수 있는 텍스트 파일 확장자
SUPPORTED_EXTENSIONS = ('.txt', '.py', '.c', '.cpp', '.java', '.js', '.html', 
                        '.css', '.md', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
                        '.sh', '.rb', '.go', '.rs', '.csv', '.tsv', '.log')

DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_IMAGE_MODEL = os.environ.get("LLM_DEFAULT_IMAGE_MODEL")
DEFAULT_ROUTE_MODEL = os.environ.get("LLM_ROUTE_MODEL") or os.environ.get("LLM_MEMORY_MODEL")
DEFAULT_ACTION_ROUTER_ENABLED = is_enabled(os.environ.get("LLM_ENABLE_ACTION_ROUTER", "0"))
DOC_CACHE_AUTO_REPLAY_WINDOW = 2
DEFAULT_PROVIDER_POLICY = "balanced"
MEMORY_UPDATE_WINDOW = 8
CONTEXT_COMPRESSION_MIN_CHARS = int(os.environ.get("LLM_CONTEXT_COMPRESSION_MIN_CHARS", "12000"))
TIMING_DEBUG_LOGS = is_enabled(os.environ.get("LLM_TIMING_DEBUG", "1"))
PROVIDER_POLICIES = ("balanced", "cheap", "fast", "private")
PROVIDER_POLICY_ALIASES = {"stable": "private"}

IMAGE_ACTION_NOUN_RE = re.compile(
    r"(이미지|그림|사진|짤|일러스트|삽화|캐릭터|배경|썸네일|아이콘|로고|아바타|프로필|만화|웹툰|"
    r"\bimage\b|\bpicture\b|\bphoto\b|\billustration\b|\bdrawing\b|\blogo\b|\bicon\b|\bthumbnail\b|\bmeme\b|\bavatar\b|\bwallpaper\b)",
    re.IGNORECASE,
)
IMAGE_ACTION_VERB_RE = re.compile(
    r"(그려|그림으로|이미지로|사진처럼|짤로|생성|만들|제작|렌더|편집|수정|변환|합성|지워|넣어|추가|바꿔|"
    r"\bgenerate\b|\bdraw\b|\bcreate\b|\bmake\b|\brender\b|\bedit\b|\bmodify\b|\btransform\b|\bremove\b|\badd\b)",
    re.IGNORECASE,
)
IMAGE_DRAW_REQUEST_RE = re.compile(r"(그려\s*(줘|봐|라)?|\bdraw\b|\bsketch\b)", re.IGNORECASE)
CURRENT_IMAGE_EDIT_RE = re.compile(
    r"(이거|이것|저거|저것|첨부|사진|이미지|그림|짤|reference|attached|this|that).{0,40}"
    r"(편집|수정|변환|합성|지워|넣어|추가|바꿔|스타일|풍으로|배경|edit|modify|transform|remove|add|style|background)",
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
- If web search is enabled for the request, use the available search tool for up-to-date information when needed. Otherwise rely on your training knowledge."""

SYSTEM_PROMPT = f"""
You are an AI member of this Discord server.

## User Identification
- Each user message starts with separate metadata lines: [사용자:display_name] and [ID:numeric_id].
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
RECENT_IMAGE_DIR = os.environ.get("LLM_RECENT_IMAGE_DIR", os.path.join(os.path.dirname(CONFIG_FILE), "llm_recent_images"))
RECENT_IMAGE_INDEX_FILE = os.path.join(RECENT_IMAGE_DIR, "index.json")
RECENT_IMAGE_LIMIT = 2
HISTORY_DEBUG_LOGS = False

IMAGE_ROUTE_SCHEMA = {
    "name": "image_route_decision",
    "strict": True,
    "schema": {
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
        "additionalProperties": False,
    },
}

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
        safe_name = re.sub(r"[^a-z0-9_]+", "_", os.path.splitext((filename or "image").lower())[0]).strip("_") or "image"
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


def log_history_debug(message):
    if HISTORY_DEBUG_LOGS:
        print(message)


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
        text = extract_pdf_text(raw_bytes)
        return text, "pdf"
    if filename.endswith(".docx"):
        text = extract_docx_text(raw_bytes)
        return text, "docx"
    if filename.endswith(".xlsx"):
        text = extract_xlsx_text(raw_bytes)
        return text, "xlsx"
    if filename.endswith(".csv"):
        text = extract_csv_text(raw_bytes, delimiter=",")
        return text, "csv"
    if filename.endswith(".tsv"):
        text = extract_csv_text(raw_bytes, delimiter="\t")
        return text, "tsv"
    if filename.endswith(SUPPORTED_EXTENSIONS):
        decoded = decode_text_bytes(raw_bytes)
        return trim_document_text(decoded), "text"
    return None, None


def sanitize_for_memory_excerpt(text):
    cleaned = re.sub(
        r"\[(?:Attached File Content|Current File|Webpage Content):[^\]]+\]\s*```.*?```",
        "[Large attached content omitted]",
        text or "",
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MEMORY_EXCERPT_MAX_CHARS]


def clamp_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = HISTORY_LIMIT
    return max(MIN_HISTORY_LIMIT, min(MAX_HISTORY_LIMIT, limit))


def get_history_limit(config):
    return clamp_history_limit((config or {}).get("history_limit", HISTORY_LIMIT))


def get_bot_display_name():
    user = getattr(bot, "user", None)
    return getattr(user, "display_name", None) or getattr(user, "name", None) or "Bot"


def get_bot_username():
    user = getattr(bot, "user", None)
    return getattr(user, "name", None) or get_bot_display_name()


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


def format_user_identity(guild, user):
    return f"[사용자:{get_server_display_name(guild, user)}]\n[ID:{getattr(user, 'id', 'unknown')}]"


def normalize_model_id(model_id):
    return (model_id or "").strip()

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
        self.models_source = "unknown"

    async def fetch_models_metadata(self):
        try:
            print("⏳ OpenRouter 모델 리스트 동기화 중...")
            async with aiohttp.ClientSession() as session:
                endpoints = [
                    (f"{self.base_url}/models/user?output_modalities=all", "user-filtered-all"),
                    (f"{self.base_url}/models/user", "user-filtered"),
                    (f"{self.base_url}/models?output_modalities=all", "global-all-fallback"),
                    (f"{self.base_url}/models", "global-fallback"),
                ]
                for url, source in endpoints:
                    async with session.get(url, headers=self.headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            raw_list = data.get('data', [])
                            raw_list.sort(key=lambda x: x.get('created') or 0, reverse=True)
                            self.models_list = raw_list
                            self.models_source = source
                            print(f"📚 {len(self.models_list)}개 모델 로드 완료 ({source})")
                            return
                        print(f"⚠️ 모델 정보 로드 실패 ({source}): {resp.status}")
        except Exception as e:
            print(f"⚠️ 모델 정보 로드 중 오류: {e}")

    def get_model_info(self, model_id):
        clean_id = normalize_model_id(model_id)
        for m in self.models_list:
            if m['id'] == clean_id: return m
        return None

    async def resolve_model_info(self, model_id):
        clean_id = normalize_model_id(model_id)
        model = self.get_model_info(clean_id)
        if model:
            return model
        detailed = await self.fetch_model_details(clean_id)
        if detailed:
            return detailed
        return {"id": clean_id, "name": clean_id}

    def _capabilities_from_model(self, model):
        if not model:
            return {"input": ["text"], "output": ["text"]}

        input_mod = model.get("input_modalities")
        output_mod = model.get("output_modalities")

        if input_mod and output_mod:
            return {"input": input_mod, "output": output_mod}

        arch = model.get('architecture', {})
        arch_input_mod = arch.get("input_modalities")
        arch_output_mod = arch.get("output_modalities")
        if arch_input_mod and arch_output_mod:
            return {"input": arch_input_mod, "output": arch_output_mod}

        modality = arch.get('modality', 'text->text').lower()
        parts = modality.split('->')
        inp = parts[0] if parts else 'text'
        out = parts[1] if len(parts) > 1 else 'text'
        input_list = [m.strip() for m in inp.split('+')]
        output_list = [m.strip() for m in out.split('+')]
        return {"input": input_list, "output": output_list}

    async def fetch_model_details(self, model_id):
        clean_id = normalize_model_id(model_id)
        if "/" not in clean_id:
            return None

        author, slug = clean_id.split("/", 1)
        url = f"{self.base_url}/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return data.get("data")
        except Exception:
            return None

    async def fetch_model_endpoints(self, model_id):
        details = await self.fetch_model_details(model_id)
        if not details:
            return None
        return details.get("endpoints", [])

    def analyze_model_capabilities(self, model_id, model_info=None):
        """모델의 입출력 capability 반환: {"input": [...], "output": [...]}"""
        clean_id = normalize_model_id(model_id)
        model = model_info or self.get_model_info(clean_id)
        return self._capabilities_from_model(model)

    def analyze_model_type(self, model_id, model_info=None):
        caps = self.analyze_model_capabilities(model_id, model_info=model_info)
        if "image" in caps["input"]:
            return 'multimodal'
        return 'text'

    def get_type_string(self, model_id, model_info=None):
        caps = self.analyze_model_capabilities(model_id, model_info=model_info)
        parts = []
        if "image" in caps["input"]: parts.append("Vision")
        if "video" in caps["input"]: parts.append("Video")
        if "audio" in caps["input"]: parts.append("Audio")
        if "image" in caps["output"]: parts.append("Image Gen")
        if "audio" in caps["output"]: parts.append("Audio Gen")
        if parts:
            return "Multimodal (" + ", ".join(parts) + ")"
        return "Text Generation"

    def _parse_completion_output(self, data):
        import ast  # 문자열 형태의 리스트 파싱을 위해 추가

        choice = data['choices'][0]
        usage = data.get('usage', {})
        raw_content = choice['message'].get('content', '')
        msg_keys = list(choice['message'].keys())
        data_keys = [k for k in data.keys() if k not in ('id', 'object', 'created', 'model', 'choices', 'usage')]
        print(f"[DEBUG] message keys: {msg_keys}, content type: {type(raw_content).__name__}, extra data keys: {data_keys}")
        if raw_content is None:
            print(f"[DEBUG] content is None, full message: {json.dumps({k: str(v)[:200] for k, v in choice['message'].items()}, ensure_ascii=False)}")
        final_content = ""
        images = []

        if raw_content is None:
            raw_content = ''

        def _extract_images(source, keys):
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

        images.extend(_extract_images(choice['message'], ['images', 'image']))
        images.extend(_extract_images(data, ['data', 'images']))

        if isinstance(raw_content, list):
            for part in raw_content:
                if isinstance(part, dict):
                    if part.get('type') in ('text', 'output_text'):
                        final_content += part.get('text', '')
                    elif part.get('type') == 'image_url':
                        img_url = part.get('image_url', {}).get('url', '')
                        if img_url:
                            images.append(img_url)
                    elif part.get('type') == 'image':
                        src = part.get('source', {})
                        if src.get('data'):
                            mt = src.get('media_type', 'image/png')
                            images.append(f"data:{mt};base64,{src['data']}")
        elif isinstance(raw_content, str):
            stripped = raw_content.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                try:
                    parsed_list = ast.literal_eval(stripped)
                    if isinstance(parsed_list, list):
                        for part in parsed_list:
                            if isinstance(part, dict):
                                if part.get('type') in ('text', 'output_text'):
                                    final_content += part.get('text', '')
                                elif part.get('type') == 'image_url':
                                    img_url = part.get('image_url', {}).get('url', '')
                                    if img_url:
                                        images.append(img_url)
                        if not final_content and not images:
                            final_content = raw_content
                    else:
                        final_content = raw_content
                except Exception:
                    final_content = raw_content
            else:
                final_content = raw_content
        else:
            final_content = str(raw_content)

        if not final_content and not images:
            final_content = "..."

        return final_content, usage, images, data

    async def chat_completion(
        self,
        model,
        messages,
        capabilities=None,
        enable_web_search=False,
        image_config=None,
        *,
        tools=None,
        plugins=None,
        response_format=None,
        provider=None,
        require_parameter_support=False,
        cache_control=None,
    ):
        payload = {
            "model": model,
            "messages": messages,
        }

        tool_list = []
        if enable_web_search:
            tool_list.append({"type": "openrouter:web_search"})
        if tools:
            tool_list.extend(copy.deepcopy(tools))
        if tool_list:
            payload["tools"] = tool_list
            payload["tool_choice"] = "auto"

        plugin_list = []
        if should_use_context_compression(messages):
            plugin_list.append({"id": "context-compression"})
        if plugins:
            plugin_list.extend(copy.deepcopy(plugins))
        if plugin_list:
            payload["plugins"] = plugin_list

        if capabilities and "image" in capabilities.get("output", []):
            if "text" in capabilities.get("output", []):
                payload["modalities"] = ["image", "text"]
            else:
                payload["modalities"] = ["image"]

        if image_config:
            payload["image_config"] = image_config
        if response_format:
            payload["response_format"] = response_format
        if provider:
            payload["provider"] = copy.deepcopy(provider)
            if require_parameter_support:
                payload["provider"]["require_parameters"] = True
        if cache_control:
            payload["cache_control"] = cache_control

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", headers=self.headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    final_content, usage, images, full_data = self._parse_completion_output(data)
                    return final_content, usage, images, full_data
                error_text = await resp.text()
                raise Exception(f"API Error ({resp.status}): {error_text}")

# ================= 4. 봇 클래스 =================
class OpenRouterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.or_client = OpenRouterClient(OPENROUTER_API_KEY)
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}
        self.doc_cache = DocumentCache(DOC_CACHE_DIR)
        self.recent_image_store = RecentImageStore(RECENT_IMAGE_DIR, RECENT_IMAGE_INDEX_FILE, limit=RECENT_IMAGE_LIMIT)

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
                "fallback_model": DEFAULT_IMAGE_MODEL,
                "reset_time": None,
                "search": False,
                "provider_policy": DEFAULT_PROVIDER_POLICY,
                "provider_order": [],
                "history_limit": HISTORY_LIMIT,
                "action_router": DEFAULT_ACTION_ROUTER_ENABLED,
                "route_model": DEFAULT_ROUTE_MODEL,
            }
        self.server_configs[gid].setdefault("fallback_model", DEFAULT_IMAGE_MODEL)
        self.server_configs[gid].setdefault("provider_policy", DEFAULT_PROVIDER_POLICY)
        self.server_configs[gid].setdefault("provider_order", [])
        self.server_configs[gid].setdefault("history_limit", HISTORY_LIMIT)
        self.server_configs[gid].setdefault("action_router", DEFAULT_ACTION_ROUTER_ENABLED)
        self.server_configs[gid].setdefault("route_model", DEFAULT_ROUTE_MODEL)
        return self.server_configs[gid]

    def get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)

bot = OpenRouterBot()

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


async def download_binary(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read(), resp.headers.get("Content-Type")
    except Exception:
        pass
    return None, None


def guess_image_mime(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def build_recent_image_hint(guild_id, *, channel_id=None):
    return (
        "Recent stored images (actual files can be reattached by the app if you request visual inspection or editing):\n"
        f"{bot.recent_image_store.describe_recent(guild_id, channel_id=channel_id)}"
    )


async def load_recent_image_inputs(guild_id, *, channel_id=None, limit=RECENT_IMAGE_LIMIT):
    inputs = []
    for entry in reversed(bot.recent_image_store.list_recent(guild_id, channel_id=channel_id)[-limit:]):
        try:
            with open(entry["path"], "rb") as f:
                raw = f.read()
            mime = guess_image_mime(entry.get("filename", ""))
            b64 = base64.b64encode(raw).decode("utf-8")
            inputs.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        except Exception as e:
            print(f"⚠️ 최근 이미지 로드 실패: {e}")
    return inputs

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

async def send_generated_images(send_func, images, filename_prefix="generated", on_sent=None):
    sent = 0
    for idx, img_data_url in enumerate(images):
        try:
            if img_data_url.startswith("http://") or img_data_url.startswith("https://"):
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_data_url) as resp:
                        if resp.status != 200:
                            raise Exception(f"remote image fetch failed: {resp.status}")
                        binary = await resp.read()
                        mime = resp.headers.get("Content-Type", "image/png")
                        ext = mime.split("/")[-1] if "/" in mime else "png"
            elif img_data_url.startswith("data:"):
                header, b64_data = img_data_url.split(",", 1)
                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                ext = mime.split("/")[-1] if "/" in mime else "png"
                binary = base64.b64decode(b64_data)
            else:
                b64_data = img_data_url
                ext = "png"
                binary = base64.b64decode(b64_data)
            await send_func(file=discord.File(fp=io.BytesIO(binary), filename=f"{filename_prefix}_{idx}.{ext}"))
            if on_sent:
                await on_sent(binary, ext, idx)
            sent += 1
        except Exception as img_err:
            print(f"⚠️ 이미지 전송 실패: {img_err}")
    return sent

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

async def ensure_model_cache_loaded():
    if not bot.or_client.models_list:
        await bot.or_client.fetch_models_metadata()

async def build_model_choices(current: str, predicate=None) -> List[app_commands.Choice[str]]:
    await ensure_model_cache_loaded()

    models = bot.or_client.models_list
    current_lower = current.lower()
    filtered = [
        m for m in models
        if (current_lower in m['id'].lower() or current_lower in m['name'].lower())
        and (predicate(m) if predicate else True)
    ]

    choices = []
    for m in filtered[:25]:
        name_display = f"{m['name']} ({format_date(m.get('created'))})"
        if len(name_display) > 100:
            name_display = name_display[:100]
        choices.append(app_commands.Choice(name=name_display, value=m['id']))
    return choices

def count_image_generation_models():
    return sum(
        1
        for model in bot.or_client.models_list
        if "image" in bot.or_client.analyze_model_capabilities(model["id"], model_info=model)["output"]
    )

async def resolve_model_bundle(model_id):
    info = await bot.or_client.resolve_model_info(model_id)
    endpoints = await bot.or_client.fetch_model_endpoints(model_id)
    caps = bot.or_client.analyze_model_capabilities(model_id, model_info=info)
    return info, endpoints, caps

async def validate_model_for_use(model_id, *, require_input=None, require_output=None, require_endpoint_check=False):
    clean_id = normalize_model_id(model_id)
    info, endpoints, caps = await resolve_model_bundle(clean_id)

    if require_endpoint_check and endpoints is None:
        return None, endpoints, caps, f"`{clean_id}` 모델의 endpoint 정보를 확인하지 못했습니다. 모델 목록이 오래됐거나 OpenRouter 응답이 일시적으로 불안정할 수 있습니다."

    if endpoints is not None and len(endpoints) == 0:
        return None, endpoints, caps, f"`{clean_id}` 모델은 현재 이 API 키 기준으로 사용 가능한 OpenRouter endpoint가 없습니다."

    if require_input and require_input not in caps["input"]:
        return None, endpoints, caps, f"`{clean_id}` 모델은 `{require_input}` 입력을 지원하지 않습니다."

    if require_output and require_output not in caps["output"]:
        return None, endpoints, caps, f"`{clean_id}` 모델은 `{require_output}` 출력을 지원하지 않습니다."

    return info, endpoints, caps, None

def get_active_generation_model(config, base_caps):
    if "image" in base_caps["output"]:
        return config["model"], "current-model"
    fallback_model = config.get("fallback_model") or DEFAULT_IMAGE_MODEL
    if fallback_model:
        return fallback_model, "configured-fallback"
    return None, None


def normalize_provider_order(value):
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def normalize_provider_policy(value):
    policy = (value or DEFAULT_PROVIDER_POLICY).strip().lower()
    return PROVIDER_POLICY_ALIASES.get(policy, policy)


def build_provider_preferences(config_or_policy, *, strict=False):
    if isinstance(config_or_policy, dict):
        config = config_or_policy
        policy = normalize_provider_policy(config.get("provider_policy"))
        provider_order = normalize_provider_order(config.get("provider_order"))
    else:
        config = {}
        policy = normalize_provider_policy(config_or_policy)
        provider_order = []

    if provider_order:
        provider = {
            "order": provider_order,
            "allow_fallbacks": False,
        }
        if strict:
            provider["require_parameters"] = True
        return provider

    if policy == "cheap":
        provider = {
            "sort": "price",
            "allow_fallbacks": True,
        }
    elif policy == "fast":
        provider = {
            "sort": "latency",
            "allow_fallbacks": True,
        }
    elif policy == "private":
        provider = {
            "allow_fallbacks": True,
            "data_collection": "deny",
        }
    else:
        provider = {
            "allow_fallbacks": True,
        }
    if strict:
        provider["require_parameters"] = True
    return provider


def endpoint_tag(endpoint):
    tag = endpoint.get("tag") or endpoint.get("provider_slug") or endpoint.get("slug")
    if tag:
        return str(tag).strip().lower()
    name = endpoint.get("provider_name") or endpoint.get("name") or ""
    if ":" in name:
        name = name.split(":", 1)[0]
    return re.sub(r"[^a-z0-9/._-]+", "-", name.strip().lower()).strip("-")


def endpoint_display_name(endpoint):
    return endpoint.get("provider_name") or endpoint.get("name") or endpoint_tag(endpoint) or "unknown"


def format_endpoint_status(endpoint):
    status = endpoint.get("status")
    if status in (0, "0", "operational", "online", "ok"):
        return "OK"
    if status is None:
        return "N/A"
    return str(status)


def format_uptime(value):
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def endpoint_metric(endpoint, *keys):
    current = endpoint
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def format_latency(endpoint):
    latency = (
        endpoint.get("latency")
        or endpoint.get("latency_ms")
        or endpoint.get("latency_last_5m")
        or endpoint_metric(endpoint, "latency", "p50")
        or endpoint_metric(endpoint, "performance", "latency")
        or endpoint_metric(endpoint, "performance", "latency_p50")
    )
    if latency is None:
        return "N/A"
    try:
        numeric = float(latency)
        if numeric > 100:
            return f"{numeric:.0f}ms"
        return f"{numeric:.2f}s"
    except (TypeError, ValueError):
        return str(latency)


def format_throughput(endpoint):
    throughput = (
        endpoint.get("throughput")
        or endpoint.get("throughput_tps")
        or endpoint.get("throughput_last_5m")
        or endpoint_metric(endpoint, "throughput", "p50")
        or endpoint_metric(endpoint, "performance", "throughput")
        or endpoint_metric(endpoint, "performance", "throughput_p50")
    )
    if throughput is None:
        return "N/A"
    try:
        return f"{float(throughput):.0f}tps"
    except (TypeError, ValueError):
        return str(throughput)


def endpoint_token_value(endpoint, *keys):
    for key in keys:
        value = endpoint.get(key)
        if value is not None:
            return value
    return None


def format_endpoint_context(endpoint):
    context = endpoint_token_value(endpoint, "context_length", "context", "max_context")
    max_output = endpoint_token_value(
        endpoint,
        "max_completion_tokens",
        "max_output",
        "max_output_tokens",
        "max_tokens",
    )
    if context is None and max_output is None:
        return "ctx N/A"
    parts = []
    if context is not None:
        parts.append(f"ctx {format_tokens(context)}")
    if max_output is not None:
        parts.append(f"out {format_tokens(max_output)}")
    return "/".join(parts)


def format_endpoint_quantization(endpoint):
    quant = endpoint.get("quantization") or endpoint.get("quantization_level")
    if isinstance(quant, list):
        quant = ", ".join(str(item) for item in quant if item)
    return str(quant) if quant else None


def format_endpoint_data_policy(endpoint):
    value = (
        endpoint.get("data_collection")
        or endpoint.get("data_policy")
        or endpoint.get("dataCollection")
        or endpoint.get("zdr")
    )
    if value is True:
        return "zdr"
    if value is False or value is None:
        return None
    return str(value)


def format_endpoint_price(endpoint):
    pricing = endpoint.get("pricing") or {}
    try:
        in_p = float(pricing.get("prompt", 0)) * 1_000_000
        out_p = float(pricing.get("completion", 0)) * 1_000_000
    except (TypeError, ValueError):
        return "N/A"

    def _fmt(v):
        if v == 0:
            return "Free"
        if v < 0.001:
            return f"${v:.6f}"
        if v < 0.01:
            return f"${v:.4f}"
        if v < 1:
            return f"${v:.3f}"
        return f"${v:.2f}"

    return f"{_fmt(in_p)}/{_fmt(out_p)}"


def format_provider_mode(config):
    order = normalize_provider_order(config.get("provider_order"))
    policy = normalize_provider_policy(config.get("provider_policy"))
    if order:
        return f"direct `{', '.join(order)}` | fallback OFF"
    return f"policy `{policy}`"


def format_endpoint_lines(endpoints, *, selected_tags=None, limit=10):
    selected_tags = set(selected_tags or [])
    if endpoints is None:
        return "endpoint 정보를 가져오지 못했습니다."
    if not endpoints:
        return "사용 가능한 endpoint가 없습니다."

    def _sort_key(endpoint):
        uptime = endpoint.get("uptime_last_30m")
        try:
            uptime_value = float(uptime)
        except (TypeError, ValueError):
            uptime_value = -1.0
        selected = endpoint_tag(endpoint) in selected_tags
        return (selected, uptime_value)

    lines = []
    for endpoint in sorted(endpoints, key=_sort_key, reverse=True)[:limit]:
        tag = endpoint_tag(endpoint) or "unknown"
        marker = "★ " if tag in selected_tags else ""
        extras = [
            format_endpoint_context(endpoint),
            format_endpoint_quantization(endpoint),
            format_endpoint_data_policy(endpoint),
        ]
        extras = [item for item in extras if item]
        lines.append(
            f"{marker}`{tag}` | {endpoint_display_name(endpoint)} | "
            f"up {format_uptime(endpoint.get('uptime_last_30m'))} | "
            f"lat {format_latency(endpoint)} | {format_throughput(endpoint)} | "
            f"status {format_endpoint_status(endpoint)} | {format_endpoint_price(endpoint)}"
            + (f" | {' | '.join(extras)}" if extras else "")
        )
    if len(endpoints) > limit:
        lines.append(f"... 외 {len(endpoints) - limit}개")
    return "\n".join(lines)


def build_prompt_cache_control(model_id, messages):
    clean_id = normalize_model_id(model_id)
    if not clean_id.startswith("anthropic/"):
        return None
    total_text = estimate_message_text_chars(messages)
    if total_text < 2048:
        return None
    return {"type": "ephemeral"}


def estimate_message_text_chars(messages):
    total_text = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            total_text += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_text += len(part.get("text", ""))
    return total_text


def should_use_context_compression(messages):
    return CONTEXT_COMPRESSION_MIN_CHARS > 0 and estimate_message_text_chars(messages) >= CONTEXT_COMPRESSION_MIN_CHARS


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
    if allow_recent_images and has_recent_images and has_recent_image_reference_signal(text):
        return True
    return False


async def decide_message_action(config, history_messages, current_user_text, guild_id, *, channel_id=None, has_current_images=False):
    route_model = normalize_model_id((config or {}).get("route_model") or DEFAULT_ROUTE_MODEL or (config or {}).get("model") or DEFAULT_MODEL)
    excerpt = build_recent_conversation_excerpt(history_messages, current_user_text)
    recent_images = bot.recent_image_store.list_recent(guild_id, channel_id=channel_id)
    inspect_action_text = ""
    inspect_rule_text = ""
    recent_image_hint = ""
    if recent_images:
        inspect_action_text = "- inspect_recent_images: the app should reattach the stored recent images before answering.\n"
        inspect_rule_text = (
            "- Choose inspect_recent_images only when the user is asking about something visible in a previous image.\n"
            "- `use_recent_images` should be true only when the app should attach the stored recent images as references.\n"
        )
        recent_image_hint = f"{build_recent_image_hint(guild_id, channel_id=channel_id)}\n\n"
    route_prompt = (
        "You are a routing planner for a Discord AI bot.\n"
        "Decide the next action for the assistant.\n\n"
        "Available actions:\n"
        "- respond_text: normal text reply.\n"
        f"{inspect_action_text}"
        "- generate_image: the app should send a refined English prompt to the image generation model.\n\n"
        "Rules:\n"
        "- Do not choose inspect_recent_images if the current message already includes image attachments.\n"
        "- Do not infer that product names, drinks, people, or objects refer to stored images by themselves.\n"
        "- Do not choose generate_image unless the user clearly wants image creation or a visual edit/transformation.\n"
        f"{inspect_rule_text}\n"
        f"Current message has image attachments: {has_current_images}\n\n"
        f"{recent_image_hint}"
        f"Recent conversation excerpt:\n{excerpt or '(empty)'}\n\n"
        f"Current user message:\n{current_user_text}"
    )
    try:
        response_text, _, _, _ = await bot.or_client.chat_completion(
            route_model,
            [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": route_prompt},
            ],
            provider=build_provider_preferences(config),
            cache_control=build_prompt_cache_control(route_model, [
                {"content": route_prompt},
            ]),
        )
        payload = coerce_json_object(response_text)
    except Exception as e:
        print(f"⚠️ 이미지 라우팅 실패: {e}")
        payload = None

    if not payload:
        return {
            "action": "respond_text",
            "refined_prompt": "",
            "use_recent_images": False,
            "reason": "route_parse_failed",
        }
    action = payload.get("action", "respond_text")
    if action not in {"respond_text", "inspect_recent_images", "generate_image"}:
        action = "respond_text"
    if action == "inspect_recent_images" and (has_current_images or not recent_images):
        action = "respond_text"
        payload["use_recent_images"] = False
    return {
        "action": action,
        "refined_prompt": (payload.get("refined_prompt") or "").strip(),
        "use_recent_images": bool(payload.get("use_recent_images", False)),
        "reason": (payload.get("reason") or "").strip(),
    }


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


def build_recent_conversation_excerpt(history_messages, current_user_text):
    lines = []
    for message in history_messages[-MEMORY_UPDATE_WINDOW:]:
        content = message.get("content", [])
        if isinstance(content, list):
            text_blocks = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            if text_blocks:
                lines.append(sanitize_for_memory_excerpt("\n".join(text_blocks).strip()))
        elif isinstance(content, str):
            lines.append(sanitize_for_memory_excerpt(content.strip()))
    if current_user_text:
        lines.append(sanitize_for_memory_excerpt(current_user_text.strip()))
    return "\n\n".join(line for line in lines if line)


async def handle_natural_image_request(message, config, history_messages, current_user_text, *, reference_inputs=None):
    base_model_info = await bot.or_client.resolve_model_info(config["model"])
    base_caps = bot.or_client.analyze_model_capabilities(config["model"], model_info=base_model_info)
    generation_model, generation_source = get_active_generation_model(config, base_caps)
    if not generation_model:
        await message.channel.send("❌ 현재 설정에서는 채팅 이미지 생성을 사용할 수 없습니다. `/model image`로 폴백 이미지를 설정해보세요.")
        return True

    model_info, _, caps, error = await validate_model_for_use(generation_model, require_output="image")
    if error:
        await message.channel.send(f"❌ {error}")
        return True
    if reference_inputs and "image" not in caps["input"]:
        await message.channel.send(
            f"❌ `{model_info['id']}` 모델은 참조 이미지 입력을 지원하지 않습니다. `/model image`로 이미지 입력을 지원하는 모델을 선택해보세요."
        )
        return True

    excerpt = build_recent_conversation_excerpt(history_messages, "")
    prompt_parts = []
    if excerpt:
        prompt_parts.append(f"[Recent Conversation]\n{excerpt}")
    prompt_parts.append(f"[Current Request]\n{current_user_text}")
    prompt_text = "\n\n".join(part for part in prompt_parts if part)
    content = [{"type": "text", "text": prompt_text}]
    if reference_inputs:
        content.extend(reference_inputs)

    response, usage, images, _ = await bot.or_client.chat_completion(
        model_info["id"],
        [
            {
                "role": "system",
                "content": (
                    "Generate the requested image. "
                    "Use recent conversation only when directly relevant to the current visual request. "
                    "Keep any text response brief."
                ),
            },
            {"role": "user", "content": content},
        ],
        capabilities=caps,
        provider=build_provider_preferences(config),
        cache_control=build_prompt_cache_control(model_info["id"], [
            {"content": prompt_text},
        ]),
    )

    if response and response != "...":
        await send_long_message(message.channel, response)
    else:
        label = "현재 모델" if generation_source == "current-model" else "폴백 이미지 모델"
        await message.channel.send(f"🖼️ {label} `{model_info['id']}`로 이미지 생성 완료")

    async def _register(binary, ext, idx):
        bot.recent_image_store.register_bytes(
            message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=bot.user.id,
            display_name=get_bot_display_name(),
            filename=f"chat_image_{idx}.{ext}",
            image_bytes=binary,
            source_kind="bot_generated",
        )

    sent = await send_generated_images(message.channel.send, images, filename_prefix="chat_image", on_sent=_register)
    if sent == 0:
        await message.channel.send("⚠️ 이미지 생성 응답은 왔지만 실제 이미지 데이터가 없었습니다. 문장을 조금 더 구체적으로 바꿔 다시 요청해보세요.")
    return True

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

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if not message.guild:
        return
    config = bot.get_config(message.guild.id)
    if message.channel.id != config.get("channel_id"):
        return

    lock = bot.get_channel_lock(message.channel.id)
    async with lock:
        async with message.channel.typing():
            try:
                timing_start = time.perf_counter()
                timing_last = timing_start

                def timing_mark(label):
                    nonlocal timing_last
                    if not TIMING_DEBUG_LOGS:
                        return
                    now = time.perf_counter()
                    print(
                        f"[TIMING] msg={message.id} {label} "
                        f"+{(now - timing_last) * 1000:.0f}ms total={(now - timing_start) * 1000:.0f}ms"
                    )
                    timing_last = now

                base_model = config["model"]
                base_model_info = await bot.or_client.resolve_model_info(base_model)
                base_caps = bot.or_client.analyze_model_capabilities(base_model, model_info=base_model_info)
                current_model = base_model
                caps = base_caps
                can_see_image = "image" in caps["input"]
                can_see_video = "video" in caps["input"]
                can_see_audio = "audio" in caps["input"]
                can_gen_image = "image" in caps["output"]
                action_router_enabled = is_enabled(config.get("action_router", DEFAULT_ACTION_ROUTER_ENABLED))
                image_command_model, _ = get_active_generation_model(config, base_caps)
                has_chat_image_support = action_router_enabled and bool(image_command_model)
                current_display_name = get_server_display_name(message.guild, message.author)
                print(f"[CAPS] {current_model}: in={caps['input']} out={caps['output']}")
                timing_mark("caps")

                sys_prompt = SYSTEM_PROMPT
                capability_hints = []
                if can_see_image:
                    capability_hints.append("You can analyze images attached by users.")
                if can_see_video:
                    capability_hints.append("You can analyze video files attached by users.")
                if can_see_audio:
                    capability_hints.append("You can analyze audio files attached by users.")
                if has_chat_image_support:
                    capability_hints.append("Chat image generation is available when the user explicitly asks you to create an image.")
                if config.get("search"):
                    capability_hints.append("Web search is available for up-to-date information when needed.")

                cannot_hints = []
                if not can_see_image:
                    cannot_hints.append("analyze images")
                if not can_see_video:
                    cannot_hints.append("analyze videos")
                if not can_see_audio:
                    cannot_hints.append("analyze audio")
                if not has_chat_image_support:
                    cannot_hints.append("generate images in chat")
                if cannot_hints:
                    capability_hints.append(
                        f"You CANNOT {', '.join(cannot_hints)}. If asked, politely explain the limitation and point the user to the relevant slash command if helpful."
                    )
                if capability_hints:
                    sys_prompt += "\n" + "\n".join(f"- {hint}" for hint in capability_hints)

                messages = [{"role": "system", "content": sys_prompt}]

                reset_dt = datetime.fromisoformat(config["reset_time"]) if config.get("reset_time") else None
                reply_target_id = message.reference.message_id if message.reference and message.reference.message_id else None

                img_count = 0
                vid_count = 0
                aud_count = 0
                history_buffer = []
                history_position = 0
                history_limit = get_history_limit(config)

                async for msg in message.channel.history(limit=history_limit + 10):
                    if msg.id == message.id:
                        continue
                    if msg.id > message.id and msg.author != bot.user:
                        continue
                    if reset_dt and msg.created_at < reset_dt:
                        break
                    if msg.author == bot.user and getattr(msg, "interaction_metadata", None) is not None:
                        continue

                    is_bot = msg.author == bot.user
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
                            if len(parts) > 1 and parts[0] in [get_bot_display_name(), get_bot_username(), "Bot", "Model"]:
                                clean_content = parts[1]
                        text_content = clean_content
                        for att in msg.attachments:
                            ct = att.content_type or ""
                            if ct.startswith("image"):
                                text_content += f"\n(Bot sent an image: {att.filename})"
                            elif ct.startswith("video"):
                                text_content += "\n(Bot generated a video here)"
                    else:
                        text_content = f"{format_user_identity(message.guild, msg.author)}\n{msg.content}"
                        for att in msg.attachments:
                            ct = att.content_type or ""
                            file_size = att.size or 0
                            too_large = file_size > MAX_MEDIA_BASE64_MB * 1024 * 1024

                            if ct.startswith("image"):
                                text_content += f"\n(User attached an image: {att.filename})"
                            elif ct.startswith("video") and can_see_video and vid_count < VIDEO_HISTORY_LIMIT and not too_large:
                                b64, mime = await encode_image(att.url)
                                if b64:
                                    content_list.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
                                    vid_count += 1
                                else:
                                    text_content += f"\n(User attached a video: {att.filename})"
                            elif ct.startswith("audio") and can_see_audio and aud_count < AUDIO_HISTORY_LIMIT and not too_large:
                                b64, _ = await encode_image(att.url)
                                if b64:
                                    fmt = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else "mp3"
                                    content_list.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
                                    aud_count += 1
                                else:
                                    text_content += f"\n(User attached an audio file: {att.filename})"
                            else:
                                extracted_text, doc_type = await extract_attachment_text(att)
                                if extracted_text and not bot.doc_cache.has_source(msg.id, "text_file", att.filename):
                                    bot.doc_cache.store_document("text_file", att.filename, extracted_text, msg.id, msg.channel.id)
                                    text_content += f"\n\n[Attached File Content: {att.filename}]\n```\n{extracted_text}\n```"
                                elif too_large:
                                    media_type = ct.split("/")[0] if "/" in ct else "file"
                                    text_content += f"\n(User attached a {media_type} file: {att.filename}, but it was too large to inline.)"
                                elif ct.startswith("image"):
                                    text_content += f"\n(User attached an image: {att.filename})"
                                elif ct.startswith("video"):
                                    text_content += f"\n(User attached a video: {att.filename})"
                                elif ct.startswith("audio"):
                                    text_content += f"\n(User attached an audio file: {att.filename})"
                                elif doc_type:
                                    text_content += f"\n(User attached a {doc_type} file: {att.filename})"

                    if cached_doc_blocks:
                        text_content += "\n\n" + "\n\n".join(cached_doc_blocks)

                    if is_bot and not text_content.strip() and len(content_list) == 0:
                        continue
                    content_list.append({"type": "text", "text": text_content})
                    history_buffer.insert(0, {"role": role, "content": content_list})
                    history_position += 1
                    if len(history_buffer) >= history_limit:
                        break

                messages.extend(history_buffer)
                timing_mark(f"history count={len(history_buffer)}")

                current_content = []
                current_image_inputs = []
                user_text = f"{format_user_identity(message.guild, message.author)}\n{message.content}"
                unresolved_urls = []

                urls = re.findall(r"(https?://\S+)", message.content)
                for url in urls:
                    if url.lower().endswith(".pdf"):
                        unresolved_urls.append(url)
                        continue
                    web_content = await scrap_webpage(url)
                    if web_content:
                        doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
                        print(f"[CACHE] webpage 저장: {doc_id} ({url})")
                        user_text += f"\n\n[Webpage Content: {url}]\n```\n{trim_document_text(web_content)}\n```"
                        print(f"[WEB] 웹사이트 내용 읽음: {url}")
                    else:
                        unresolved_urls.append(url)
                if unresolved_urls:
                    user_text += "\n\n[Use web fetch if needed]\n" + "\n".join(unresolved_urls)

                has_media = False
                if message.attachments:
                    for att in message.attachments:
                        ct = att.content_type or ""
                        file_size = att.size or 0
                        too_large = file_size > MAX_MEDIA_BASE64_MB * 1024 * 1024
                        print(f"[ATT] {att.filename} ct={ct} size={file_size} can_see_image={can_see_image} too_large={too_large}")

                        if ct.startswith("image") and not too_large:
                            raw, mime = await download_binary(att.url)
                            if raw:
                                b64 = base64.b64encode(raw).decode("utf-8")
                                image_part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                                current_image_inputs.append(image_part)
                                if can_see_image:
                                    current_content.append(image_part)
                                    has_media = True
                                print(f"[ATT] 이미지 base64 인코딩 완료: {att.filename} ({len(b64)} chars)")
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
                                if not can_see_image:
                                    user_text += f"\n(User attached an image: {att.filename})"
                            else:
                                user_text += f"\n(User attached an image: {att.filename})"
                        elif ct.startswith("video") and can_see_video and not too_large:
                            b64, mime = await encode_image(att.url)
                            if b64:
                                current_content.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
                                has_media = True
                            else:
                                user_text += f"\n(User attached a video: {att.filename})"
                        elif ct.startswith("audio") and can_see_audio and not too_large:
                            b64, _ = await encode_image(att.url)
                            if b64:
                                fmt = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else "mp3"
                                current_content.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
                                has_media = True
                            else:
                                user_text += f"\n(User attached an audio file: {att.filename})"
                        else:
                            extracted_text, doc_type = await extract_attachment_text(att)
                            if extracted_text:
                                doc_id = bot.doc_cache.store_document("text_file", att.filename, extracted_text, message.id, message.channel.id)
                                print(f"[CACHE] {doc_type or 'text'} file 저장: {doc_id} ({att.filename})")
                                user_text += f"\n\n[Attached File Content: {att.filename}]\n```\n{extracted_text}\n```"
                            elif too_large:
                                media_type = ct.split("/")[0] if "/" in ct else "file"
                                user_text += f"\n(User attached a {media_type} file: {att.filename}, but it's too large to process: {file_size // (1024 * 1024)}MB)"
                            elif ct.startswith("image"):
                                user_text += f"\n(User attached an image: {att.filename})"
                timing_mark(f"attachments current_images={len(current_image_inputs)}")

                recent_images_available = bool(bot.recent_image_store.list_recent(message.guild.id, channel_id=message.channel.id))
                should_route_action = action_router_enabled and should_call_action_router(
                    message.content,
                    has_current_images=bool(current_image_inputs),
                    has_recent_images=recent_images_available,
                    allow_image_generation=has_chat_image_support,
                    allow_recent_images=can_see_image,
                )

                if current_image_inputs and not can_see_image and not should_route_action:
                    await message.channel.send(
                        f"❌ `{current_model}` 모델은 이미지 분석을 지원하지 않습니다. 비전 입력을 지원하는 `/model set` 모델로 바꿔보세요."
                    )
                    return

                route = {
                    "action": "respond_text",
                    "refined_prompt": "",
                    "use_recent_images": False,
                    "reason": "action_router_skipped",
                }
                if should_route_action:
                    route = await decide_message_action(
                        config,
                        history_buffer,
                        user_text,
                        message.guild.id,
                        channel_id=message.channel.id,
                        has_current_images=bool(current_image_inputs),
                    )
                timing_mark(
                    f"route enabled={action_router_enabled} prefilter={should_route_action} action={route.get('action')}"
                )

                if route.get("action") == "generate_image":
                    reference_inputs = list(current_image_inputs)
                    if not reference_inputs and route.get("use_recent_images"):
                        reference_inputs = await load_recent_image_inputs(message.guild.id, channel_id=message.channel.id)
                    handled = await handle_natural_image_request(
                        message,
                        config,
                        history_buffer,
                        route.get("refined_prompt") or message.content,
                        reference_inputs=reference_inputs,
                    )
                    if handled:
                        return

                if current_image_inputs and not can_see_image:
                    await message.channel.send(
                        f"❌ `{current_model}` 모델은 이미지 분석을 지원하지 않습니다. 비전 입력을 지원하는 `/model set` 모델로 바꿔보세요."
                    )
                    return

                if route.get("action") == "inspect_recent_images" and not current_image_inputs and route.get("use_recent_images"):
                    if not can_see_image:
                        await message.channel.send(
                            f"❌ `{current_model}` 모델은 과거 이미지를 다시 분석할 수 없습니다. 비전 입력을 지원하는 `/model set` 모델로 바꿔보세요."
                        )
                        return
                    current_content = (await load_recent_image_inputs(message.guild.id, channel_id=message.channel.id)) + current_content

                current_content.append({"type": "text", "text": user_text})
                messages.append({"role": "user", "content": current_content})

                chat_tools = []
                if unresolved_urls:
                    chat_tools.append(
                        {
                            "type": "openrouter:web_fetch",
                            "parameters": {
                                "engine": "auto",
                                "max_uses": min(10, len(unresolved_urls)),
                                "max_content_tokens": 12000,
                            },
                        }
                    )

                provider_prefs = build_provider_preferences(config, strict=bool(chat_tools or config.get("search")))
                cache_control = build_prompt_cache_control(current_model, messages)
                timing_mark(
                    f"pre_chat chars={estimate_message_text_chars(messages)} "
                    f"plugins={'compression' if should_use_context_compression(messages) else 'none'}"
                )

                try:
                    response, usage, images, full_data = await bot.or_client.chat_completion(
                        current_model,
                        messages,
                        capabilities=caps,
                        enable_web_search=config.get("search", False),
                        tools=chat_tools or None,
                        provider=provider_prefs,
                        require_parameter_support=bool(chat_tools or config.get("search")),
                        cache_control=cache_control,
                    )
                    provider_name = full_data.get("provider") if isinstance(full_data, dict) else None
                    routed_model = full_data.get("model") if isinstance(full_data, dict) else None
                    timing_mark(f"chat provider={provider_name or '?'} model={routed_model or current_model}")
                except Exception as e:
                    timing_mark("chat_error")
                    err_msg = str(e).lower()
                    if "no endpoints found for" in err_msg:
                        print(f"⚠️ [Routing Error] {current_model}: endpoint 없음, 모델 목록 재동기화")
                        await bot.or_client.fetch_models_metadata()
                        await message.channel.send(
                            f"❌ `{current_model}` 모델은 현재 OpenRouter에서 실제 endpoint를 찾지 못했습니다.\n"
                            f"목록과 실제 라우팅 상태가 어긋났거나 provider 정책(`{config.get('provider_policy', DEFAULT_PROVIDER_POLICY)}`)으로 endpoint가 걸러졌을 수 있습니다.\n"
                            f"`/model refresh` 후 `/model set`으로 다시 선택하거나 `/model provider balanced`로 완화해보세요."
                        )
                        return

                    has_any_media = has_media or any(
                        isinstance(m.get("content"), list)
                        and any(part.get("type") in ("image_url", "video_url", "input_audio") for part in m["content"])
                        for m in messages
                    )
                    media_error_markers = (
                        "image_url",
                        "video_url",
                        "input_audio",
                        "unsupported content",
                        "unsupported input",
                        "invalid image",
                        "invalid video",
                        "invalid audio",
                        "does not support images",
                        "does not support image",
                        "does not support videos",
                        "does not support video",
                        "does not support audio",
                        "multimodal",
                        "modalit",
                        "base64",
                        "'choices'",
                    )
                    is_media_error = has_any_media and any(marker in err_msg for marker in media_error_markers)
                    if not is_media_error:
                        raise e

                    print(f"⚠️ [Media Error] {current_model}: 텍스트 모드로 전환.")
                    await message.channel.send(f"⚠️ `{current_model}` 모델이 미디어를 처리하지 못했습니다. 텍스트 모드로 재시도합니다.")
                    for payload in messages:
                        if isinstance(payload.get("content"), list):
                            payload["content"] = [part for part in payload["content"] if part.get("type") == "text"]
                    if has_media and isinstance(messages[-1].get("content"), list):
                        for part in messages[-1]["content"]:
                            if part.get("type") == "text":
                                part["text"] += "\n(System: 미디어 첨부가 있었으나 이 모델이 처리하지 못했습니다.)"
                                break
                    response, usage, images, full_data = await bot.or_client.chat_completion(
                        current_model,
                        messages,
                        enable_web_search=config.get("search", False),
                        tools=chat_tools or None,
                        provider=provider_prefs,
                        require_parameter_support=bool(chat_tools or config.get("search")),
                        cache_control=cache_control,
                    )

                bot_display_name = get_bot_display_name()
                if response.startswith(f"{bot_display_name}:"):
                    response = response.replace(f"{bot_display_name}:", "", 1).strip()

                total_tokens = usage.get("total_tokens", "N/A")
                prompt_details = usage.get("prompt_tokens_details", {}) if isinstance(usage, dict) else {}
                cached_tokens = prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0
                log_time = datetime.now().strftime("%H:%M:%S")

                print(f"[{log_time}] [{message.guild.name}] {current_display_name}: {message.content}")
                print(f"봇({current_model}): {response} (Tokens: {total_tokens}, Cached: {cached_tokens})")
                if images:
                    print(f"  [이미지 생성]: {len(images)}장")
                if isinstance(full_data, dict) and full_data.get("provider"):
                    print(f"  [provider] {full_data.get('provider')}")
                print("-------")

                if response:
                    await send_long_message(message.channel, response)
                async def _register(binary, ext, idx):
                    bot.recent_image_store.register_bytes(
                        message.guild.id,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        author_id=bot.user.id,
                        display_name=get_bot_display_name(),
                        filename=f"response_image_{idx}.{ext}",
                        image_bytes=binary,
                        source_kind="bot_generated",
                    )

                await send_generated_images(message.channel.send, images, on_sent=_register)

            except Exception as e:
                print(f"❌ 오류: {str(e)}")
                await message.channel.send(f"❌ 오류: {str(e)}")

# ================= 7. 명령어 =================

async def model_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return await build_model_choices(current)

async def image_generation_model_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return await build_model_choices(
        current,
        predicate=lambda m: "image" in bot.or_client.analyze_model_capabilities(m["id"], model_info=m)["output"],
    )

async def route_model_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    current_lower = current.lower()
    special_choices = [
        app_commands.Choice(name="off - 보조 라우터 끄기", value="off"),
        app_commands.Choice(name="current - 현재 채팅 모델 사용", value="current"),
    ]
    choices = [
        choice for choice in special_choices
        if current_lower in choice.name.lower() or current_lower in choice.value.lower()
    ]
    model_choices = await build_model_choices(
        current,
        predicate=lambda m: "text" in bot.or_client.analyze_model_capabilities(m["id"], model_info=m)["output"],
    )
    return (choices + model_choices)[:25]


async def provider_policy_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    current_lower = (current or "").lower()
    labels = {
        "balanced": "balanced - OpenRouter 기본 라우팅",
        "cheap": "cheap - 최저가 우선",
        "fast": "fast - 낮은 latency 우선",
        "private": "private - data_collection deny",
    }
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in labels.items()
        if not current_lower or current_lower in value or current_lower in label.lower()
    ][:25]


async def provider_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    current_lower = (current or "").lower()
    choices = [
        app_commands.Choice(name="auto - 직접 provider 선택 해제", value="auto"),
        app_commands.Choice(name="off - 직접 provider 선택 해제", value="off"),
    ]
    config = bot.get_config(interaction.guild_id)
    endpoints = await bot.or_client.fetch_model_endpoints(config.get("model", DEFAULT_MODEL)) or []
    for endpoint in endpoints:
        tag = endpoint_tag(endpoint)
        if not tag:
            continue
        label = (
            f"{tag} | {endpoint_display_name(endpoint)} | "
            f"up {format_uptime(endpoint.get('uptime_last_30m'))} | "
            f"lat {format_latency(endpoint)} | {format_throughput(endpoint)} | "
            f"{format_endpoint_price(endpoint)}"
        )
        if current_lower and current_lower not in label.lower() and current_lower not in tag.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=tag[:100]))
        if len(choices) >= 25:
            break
    return choices[:25]

model_group = app_commands.Group(name="model", description="모델 관련 설정")


@model_group.command(name="set", description="기본 채팅 모델 변경")
@app_commands.autocomplete(model_id=model_autocomplete)
@app_commands.describe(model_id="모델 이름 검색")
async def set_model(interaction: discord.Interaction, model_id: str):
    await interaction.response.defer()
    clean_id = normalize_model_id(model_id)
    model_info, endpoints, _, error = await validate_model_for_use(clean_id, require_endpoint_check=True)
    if error:
        await interaction.followup.send(
            f"❌ {error}\n`/model refresh` 후 다시 시도해보세요.",
        )
        return

    config = bot.get_config(interaction.guild_id)
    config["model"] = model_info['id']
    config["provider_order"] = []
    bot.save_config()
    
    type_str = bot.or_client.get_type_string(model_info['id'], model_info=model_info)
    created = format_date(model_info.get('created'))
    endpoint_suffix = f" | 엔드포인트: {len(endpoints)}개" if endpoints is not None else ""
    actor_name = get_server_display_name(interaction.guild, interaction.user)

    await interaction.followup.send(
        f"✅ {actor_name}님이 기본 모델을 변경했습니다.\n"
        f"**{model_info['name']}** (`{model_info['id']}`)\n"
        f"타입: **{type_str}** | 출시일: {created}{endpoint_suffix}",
    )

@model_group.command(name="image", description="채팅/고급 이미지 생성용 폴백 모델 설정")
@app_commands.autocomplete(model_id=image_generation_model_autocomplete)
@app_commands.describe(model_id="이미지 생성 폴백 모델 ID 또는 clear")
async def set_fallback_model(interaction: discord.Interaction, model_id: str):
    await interaction.response.defer()
    clean_id = normalize_model_id(model_id)
    config = bot.get_config(interaction.guild_id)

    if clean_id.lower() in ("clear", "none", "off"):
        config["fallback_model"] = None
        bot.save_config()
        actor_name = get_server_display_name(interaction.guild, interaction.user)
        await interaction.followup.send(f"✅ {actor_name}님이 이미지 폴백 모델 설정을 해제했습니다.")
        return

    model_info, endpoints, _, error = await validate_model_for_use(
        clean_id,
        require_output="image",
        require_endpoint_check=True,
    )
    if error:
        await interaction.followup.send(f"❌ {error}")
        return

    config["fallback_model"] = model_info["id"]
    bot.save_config()
    endpoint_suffix = f" | 엔드포인트: {len(endpoints)}개" if endpoints is not None else ""
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.followup.send(
        f"✅ {actor_name}님이 이미지 폴백 모델을 설정했습니다.\n"
        f"**{model_info['name']}** (`{model_info['id']}`){endpoint_suffix}",
    )


@model_group.command(name="router", description="자연어 이미지 라우터 보조 모델 설정")
@app_commands.autocomplete(model_id=route_model_autocomplete)
@app_commands.describe(model_id="보조 라우터 모델 ID, current, off/clear")
async def set_route_model(interaction: discord.Interaction, model_id: str | None = None):
    config = bot.get_config(interaction.guild_id)
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    current_route = config.get("route_model") or config.get("model") or DEFAULT_MODEL

    if model_id is None:
        state = "ON" if is_enabled(config.get("action_router", DEFAULT_ACTION_ROUTER_ENABLED)) else "OFF"
        await interaction.response.send_message(
            f"현재 보조 라우터: **{state}**\n"
            f"모델: `{current_route}`"
        )
        return

    await interaction.response.defer()
    clean_id = normalize_model_id(model_id)
    if clean_id.lower() in ("clear", "none", "off", "disable", "disabled"):
        config["action_router"] = False
        config["route_model"] = None
        bot.save_config()
        await interaction.followup.send(f"✅ {actor_name}님이 보조 라우터를 껐습니다.")
        return

    if clean_id.lower() in ("current", "auto", "default"):
        config["action_router"] = True
        config["route_model"] = None
        bot.save_config()
        await interaction.followup.send(
            f"✅ {actor_name}님이 보조 라우터를 현재 채팅 모델 `{config['model']}` 기준으로 켰습니다."
        )
        return

    model_info, endpoints, _, error = await validate_model_for_use(
        clean_id,
        require_output="text",
        require_endpoint_check=True,
    )
    if error:
        await interaction.followup.send(f"❌ {error}")
        return

    config["action_router"] = True
    config["route_model"] = model_info["id"]
    bot.save_config()
    endpoint_suffix = f" | 엔드포인트: {len(endpoints)}개" if endpoints is not None else ""
    await interaction.followup.send(
        f"✅ {actor_name}님이 보조 라우터 모델을 설정했습니다.\n"
        f"**{model_info['name']}** (`{model_info['id']}`){endpoint_suffix}"
    )


@bot.tree.command(name="reset", description="히스토리 초기화")
async def reset_history(interaction: discord.Interaction):
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    bot.save_config()
    await interaction.response.send_message("🧹 히스토리 초기화")


@model_group.command(name="provider", description="프로바이더 정책 확인/직접 선택")
@app_commands.autocomplete(policy=provider_policy_autocomplete, provider=provider_autocomplete)
@app_commands.describe(
    policy="balanced/cheap/fast/private. 지정하면 직접 provider 선택을 해제하고 정책 모드로 전환",
    provider="직접 사용할 provider tag. auto/off/clear는 직접 선택 해제",
)
async def set_provider_policy(
    interaction: discord.Interaction,
    policy: str | None = None,
    provider: str | None = None,
):
    await interaction.response.defer()
    config = bot.get_config(interaction.guild_id)
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    changed = []

    if policy:
        clean_policy = normalize_provider_policy(policy)
        if clean_policy not in PROVIDER_POLICIES:
            await interaction.followup.send(
                f"❌ policy는 `{', '.join(PROVIDER_POLICIES)}` 중 하나여야 합니다."
            )
            return
        config["provider_policy"] = clean_policy
        config["provider_order"] = []
        changed.append(f"정책 `{clean_policy}`")

    if provider:
        clean_provider = provider.strip().lower()
        if clean_provider in ("auto", "clear", "none", "off", "disable", "disabled"):
            config["provider_order"] = []
            changed.append("직접 provider 해제")
        else:
            endpoints = await bot.or_client.fetch_model_endpoints(config.get("model", DEFAULT_MODEL))
            available_tags = {endpoint_tag(endpoint) for endpoint in endpoints or [] if endpoint_tag(endpoint)}
            if available_tags and clean_provider not in available_tags:
                await interaction.followup.send(
                    f"❌ `{clean_provider}` provider는 현재 모델 endpoint 목록에 없습니다.\n"
                    f"`/model provider`로 가능한 tag를 확인해보세요."
                )
                return
            config["provider_order"] = [clean_provider]
            changed.append(f"직접 provider `{clean_provider}`")

    if changed:
        bot.save_config()

    endpoints = await bot.or_client.fetch_model_endpoints(config.get("model", DEFAULT_MODEL))
    selected_tags = normalize_provider_order(config.get("provider_order"))
    embed = discord.Embed(title="🛰️ Provider Routing", color=0x4caf50)
    embed.add_field(name="Current", value=format_provider_mode(config), inline=False)
    embed.add_field(
        name=f"Endpoints for `{config.get('model', DEFAULT_MODEL)}`",
        value=format_endpoint_lines(endpoints, selected_tags=selected_tags, limit=12)[:1024],
        inline=False,
    )
    footer = "변경 없음. provider 또는 policy 옵션을 넣으면 설정됩니다."
    if changed:
        footer = f"{actor_name}: " + ", ".join(changed)
    embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)


@model_group.command(name="history", description="대화 원문 히스토리 참조 개수 설정")
@app_commands.describe(count=f"참고할 최근 메시지 개수 ({MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)")
async def set_history_limit(interaction: discord.Interaction, count: int | None = None):
    config = bot.get_config(interaction.guild_id)
    if count is None:
        await interaction.response.send_message(
            f"현재 원문 히스토리 참조: **{get_history_limit(config)}개** "
            f"(범위 {MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)"
        )
        return
    limit = clamp_history_limit(count)
    config["history_limit"] = limit
    bot.save_config()
    suffix = "" if limit == count else f" (요청값 {count}에서 허용 범위로 보정)"
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"✅ {actor_name}님이 원문 히스토리 참조를 **{limit}개**로 설정했습니다.{suffix}"
    )

@bot.tree.command(name="dashboard", description="현재 모델 스펙 확인")
async def dashboard(interaction: discord.Interaction):
    await interaction.response.defer()
    config = bot.get_config(interaction.guild_id)
    curr_model = config['model']
    
    if not bot.or_client.models_list:
        await bot.or_client.fetch_models_metadata()
        
    model_meta = bot.or_client.get_model_info(curr_model) or {}
    if not model_meta:
        model_meta = await bot.or_client.fetch_model_details(curr_model) or {}
    type_str = bot.or_client.get_type_string(curr_model, model_info=model_meta)
    caps = bot.or_client.analyze_model_capabilities(curr_model, model_info=model_meta)
    is_multimodal = len(caps["input"]) > 1 or len(caps["output"]) > 1
    endpoints = await bot.or_client.fetch_model_endpoints(curr_model)

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
    if endpoints is not None:
        embed.add_field(name="🛰️ Routing", value=f"• **Available Endpoints:** {len(endpoints)}", inline=True)

    action_router_enabled = is_enabled(config.get("action_router", DEFAULT_ACTION_ROUTER_ENABLED))
    route_model_status = config.get("route_model") or "current chat model"
    image_command_model, image_command_source = get_active_generation_model(config, caps)
    if not action_router_enabled:
        image_command_status = "Natural chat routing OFF"
    elif not image_command_model:
        image_command_status = "Unavailable"
    elif image_command_source == "current-model":
        image_command_status = "Uses current model"
    else:
        image_command_status = f"Uses fallback `{image_command_model}`"

    helpers = [
        f"• **Search:** {'ON (server tool)' if config.get('search') else 'OFF'}",
        f"• **Action Router:** {'ON' if action_router_enabled else 'OFF'}",
        f"• **Router Model:** `{route_model_status}`",
        f"• **Chat Image:** {image_command_status}",
        f"• **Image Models:** {count_image_generation_models()} discovered",
        f"• **Provider:** {format_provider_mode(config)}",
        f"• **History:** {get_history_limit(config)} messages",
    ]
    embed.add_field(name="🧩 Helpers", value="\n".join(helpers), inline=False)
    if endpoints is not None:
        endpoint_lines = format_endpoint_lines(
            endpoints,
            selected_tags=normalize_provider_order(config.get("provider_order")),
            limit=8,
        )
        embed.add_field(name="🛰️ Provider Endpoints", value=endpoint_lines[:1024], inline=False)

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

@bot.tree.command(name="search", description="웹 검색 기능 ON/OFF")
async def toggle_search(interaction: discord.Interaction):
    config = bot.get_config(interaction.guild_id)
    config["search"] = not config.get("search", False)
    bot.save_config()
    state = "ON 🔍" if config["search"] else "OFF"
    await interaction.response.send_message(
        f"✅ 웹 검색: **{state}**\n방식: `openrouter:web_search` server tool\n기본 모델: `{config['model']}`"
    )

@model_group.command(name="refresh", description="OpenRouter 모델 리스트 즉시 갱신")
async def refresh_models_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    before = len(bot.or_client.models_list)
    await bot.or_client.fetch_models_metadata()
    after = len(bot.or_client.models_list)
    image_count = count_image_generation_models()
    if after == 0:
        await interaction.followup.send("[ERROR] 모델 리스트 갱신 실패 — OpenRouter API 응답 확인 필요")
    elif after == before:
        await interaction.followup.send(
            f"✅ 갱신 완료 (변동 없음, {after}개 모델 / 이미지 출력 {image_count}개)",
        )
    else:
        delta = after - before
        sign = "+" if delta > 0 else ""
        await interaction.followup.send(
            f"✅ 갱신 완료: {before}개 → **{after}개** ({sign}{delta}) / 이미지 출력 {image_count}개",
        )

bot.tree.add_command(model_group)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
