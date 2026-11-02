import discord
from discord import app_commands
from discord.ext import commands
import openai
import aiohttp
import asyncio
import base64
import bisect
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

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

# ================= 1. 설정 및 상수 =================
DISCORD_TOKEN = os.environ["GPT_DISCORD_TOKEN"]
# 답변과 사전 검사(무료 OpenAI Moderation)에 같은 키를 쓴다.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY 환경 변수가 필요합니다.")
# 웹 검색·링크 본문 추출은 Tavily. 키가 없거나 Tavily가 안 되면 OpenAI 기본 검색(1회 $0.01)으로 대신한다.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
TAVILY_MAX_RESULTS = 5  # 모델이 결과 수를 정하지 않았을 때 쓰는 값
TAVILY_MAX_RESULTS_CAP = 10  # 모델이 고를 수 있는 결과 수 상한 (토큰 안전장치)
TAVILY_RESULT_CHARS = 2000  # 결과 하나당 본문 최대 길이 (토큰 안전장치)
TAVILY_OUTAGE_COOLDOWN = 600  # Tavily 키·크레딧·서버 문제가 나면 이 시간(초) 동안 OpenAI 기본 검색을 쓴다
WEB_SEARCH_MAX_CALLS = 5  # 한 메시지에서 검색 호출 총 상한
WEB_SEARCH_MAX_ROUNDS = WEB_SEARCH_MAX_CALLS  # 한 번에 하나씩 검색해도 한도를 다 쓸 수 있게 왕복 수를 맞춘다
SEARCH_MODES = {"tavily": "Tavily", "openai": "OpenAI 기본 검색"}
DEFAULT_SEARCH_MODE = "tavily"

# 가격은 100만 토큰당 달러: (입력, 출력, 캐시 읽기, 캐시 쓰기 30분). 로그에 찍는 예상 비용 계산용이다.
# 사고 강도를 비우면 모델 기본값. none은 Terra·Luna만 받는다.
EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
MODELS = {
    "luna": {
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "efforts": EFFORT_LEVELS,
        "price": (0.10, 0.50, 0.01, 0.125),
    },
    "terra": {
        "id": "gpt-6.1-terra",
        "name": "GPT-6.1 Terra",
        "efforts": EFFORT_LEVELS,
        "price": (1.0, 5.0, 0.05, 1.25),
    },
    "sol": {
        "id": "gpt-6.1-sol",
        "name": "GPT-6.1 Sol",
        "efforts": EFFORT_LEVELS[1:],
        "price": (2.0, 10.0, 0.10, 2.50),
    },
    "astra": {
        "id": "gpt-6-astra",
        "name": "GPT-6 Astra",
        "efforts": EFFORT_LEVELS[1:],
        "price": (10.0, 50.0, 1.0, 12.50),
    },
}
DEFAULT_MODEL_KEY = "terra"
MAX_OUTPUT_TOKENS = 25000  # 사고 토큰도 여기에 포함된다
MAX_OUTPUT_TOKENS_DEEP = 64000  # 사고 강도 xhigh·max
NATIVE_SEARCH_PRICE = 0.01  # OpenAI 기본 웹 검색 1회 요금

# 이미지 생성·수정은 hosted image_generation 도구. 그릴지, 새로 그릴지 고칠지는 대화 모델이 정한다.
IMAGE_MODELS = {
    "flare": {"id": "gpt-image-2.5-flare", "name": "GPT Image 2.5 Flare (빠른 일반 생성)"},
    "sunburst": {"id": "gpt-image-2.5-sunburst", "name": "GPT Image 2.5 Sunburst (정밀 수정)"},
}
DEFAULT_IMAGE_MODEL_KEY = "flare"
IMAGE_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
DEFAULT_IMAGE_QUALITY = "medium"
IMAGE_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
DEFAULT_IMAGE_SIZE = "auto"
IMAGE_TOKEN_PRICE = (5.0, 8.0, 30.0)  # 100만 토큰당: 텍스트 입력, 이미지 입력, 이미지 출력
IMAGE_MAX_PER_MESSAGE = 4  # 메시지 하나에서 만드는 이미지 상한 (비용 안전장치)

# 🔢 [수치 조절 구역]
# 히스토리는 블록 단위로 자른다: 창이 HISTORY_LIMIT개에서 시작해 HISTORY_LIMIT + 블록 - 1개까지 뒤로만 늘고,
# 그다음 메시지에서 오래된 블록 하나를 한 번에 버린다. 블록 동안 앞부분이 그대로라 프롬프트 캐시가 맞는다.
HISTORY_LIMIT = 20
MIN_HISTORY_LIMIT = 1
MAX_HISTORY_LIMIT = 100
HISTORY_BLOCK = 20
MIN_HISTORY_BLOCK = 1
MAX_HISTORY_BLOCK = 100
# 창 안 이미지는 전부 다시 보낸다. 이 개수를 넘으면 블록을 일찍 교체한다 (0이면 히스토리 이미지 끔).
IMAGE_HISTORY_LIMIT = int(os.environ.get("GPT_IMAGE_HISTORY_LIMIT", "10"))
MIN_IMAGE_HISTORY_LIMIT = 0
MAX_IMAGE_HISTORY_LIMIT = 20
# 이미지는 변 2000px 안으로 맞춘다 (넘으면 줄여서 JPEG로 다시 저장).
IMAGE_MAX_EDGE = 2000
IMAGE_MAX_BYTES = 2 * 1024 * 1024  # 이보다 큰 이미지도 JPEG로 다시 저장
IMAGE_SOURCE_MAX_BYTES = 25 * 1024 * 1024  # 원본이 이보다 크면 받지 않는다
# 요청 한도(이미지 합계 50MB) 안에 히스토리 + 현재 메시지 이미지가 넉넉히 들어가게 나눠 둔다.
HISTORY_MEDIA_BYTES_CAP = 10 * 1024 * 1024
CURRENT_MEDIA_BYTES_CAP = 10 * 1024 * 1024
IMAGE_CACHE_BYTES = 64 * 1024 * 1024  # 히스토리 이미지를 메시지마다 다시 받지 않게 메모리에 두는 양
MAX_DOC_CHARS = 12000
MAX_DOC_SOURCE_BYTES = 20 * 1024 * 1024
MAX_LINKS_PER_MESSAGE = 5
MIN_SCRAPED_CHARS = 200  # 직접 읽은 본문이 이보다 짧으면(자바스크립트 페이지 등) Tavily로 다시 읽는다
MAX_SCRAPE_BYTES = 3 * 1024 * 1024  # 직접 읽을 때 받는 HTML 상한

# 읽을 수 있는 텍스트 파일 확장자
SUPPORTED_EXTENSIONS = ('.txt', '.py', '.c', '.cpp', '.java', '.js', '.html',
                        '.css', '.md', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
                        '.sh', '.rb', '.go', '.rs', '.csv', '.tsv', '.log')
DOCUMENT_EXTENSIONS = SUPPORTED_EXTENSIONS + ('.pdf', '.docx', '.xlsx')
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# 히스토리에서 빼는 봇 안내 메시지 (오류 등)
BOT_NOTICE_PREFIXES = ("오류가 발생했습니다", "⚠️")
OWN_IMAGE_LABEL = "(Image you generated earlier)"  # 봇이 만든 이미지를 히스토리로 다시 보낼 때 붙이는 말

# 시스템 프롬프트·대화 기록 모양은 Gemini 봇(NRGemini/gemini.py)·LLM 봇과 같게 유지한다 (말투가 같도록).
_COMMON_RULES = """## General Rules
- Respond in the user's language and mirror their tone (casual, formal, vulgar — match them).
- If the user sets up a specific roleplay scenario (character, world, setting), maintain it consistently within that conversation. Do NOT treat casual chat as "established context" that must persist.
- Analyze attached files (images/video/audio) when supported.
- Keep responses concise and Discord-appropriate."""

HISTORY_TIME_RULE = (
    "- Earlier user messages are labeled `Name (MM/DD HH:MM):` with the time they were sent (KST). Use it to tell "
    "how long ago something was said. Do not copy this label format in your replies."
)

CURRENT_TIME_RULE = (
    "- The latest message begins with a `[System time: YYYY-MM-DD (Day) HH:MM KST — …]` line. The bot inserts it "
    "automatically; the user did not write it. It is the real current date and time; your training data is older, "
    "so do not assume an earlier year."
)

SYSTEM_PROMPT = f"""
You are an AI member of this Discord server.

## Current Time
{CURRENT_TIME_RULE}
{HISTORY_TIME_RULE}

## Conversation Context
- Chat history is for CONTEXT ONLY — to understand the flow, not to recycle.
- When the topic changes, focus ONLY on the current topic. Do NOT drag in old topics.
- Do NOT reuse specific words, jokes, memes, or references from previous conversations to fake familiarity. Every new topic gets a fresh start.
- User's background info (job, interests, etc.) is for matching TONE only. Do NOT shoehorn it into answers unless the user explicitly brings it up or it has direct logical relevance to the current question.
- Answer new questions cleanly and on-point. Do NOT over-decorate answers by mixing in past context.
- You may briefly reference a previous topic AT MOST once if naturally relevant, then move on entirely.

{_COMMON_RULES}
"""

# GPT 봇 구조상 필요한 안내만 덧붙인다. 요청마다 같은 문자열이어야 프롬프트 캐시가 맞는다.
# 검색 도구는 Tavily든 OpenAI 기본 검색이든 이름이 web_search라 안내 문구가 같다.
GPT_RULES = (
    "You have a `web_search` tool. Use it for anything time-sensitive or that may have changed after your training "
    "(news, prices, releases, schedules) instead of guessing. Don't search for casual chat.",
    "You have an `image_generation` tool. Use it only when the user wants an image created or edited, and don't "
    f"describe the image at length afterwards. Make at most {IMAGE_MAX_PER_MESSAGE} images per message.",
    "Items labeled `" + OWN_IMAGE_LABEL + "` are images you generated earlier, shown so you can edit them.",
    "You cannot watch videos or listen to audio. If asked, say so.",
)
GPT_SYSTEM_PROMPT = SYSTEM_PROMPT.rstrip() + "\n" + "\n".join(f"- {rule}" for rule in GPT_RULES) + "\n"
# 명시적 캐시 지점은 최상위 instructions에 둘 수 없어 developer 메시지로 보낸다.
DEVELOPER_ITEM = {
    "role": "developer",
    "content": [{"type": "input_text", "text": GPT_SYSTEM_PROMPT, "prompt_cache_breakpoint": {"mode": "explicit"}}],
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
DATA_DIR = os.path.dirname(CONFIG_FILE)
DOC_CACHE_DIR = os.environ.get("GPT_DOC_CACHE_DIR", os.path.join(DATA_DIR, "gpt_doc_cache"))
GUARD_FILE = os.environ.get("GPT_GUARD_FILE", os.path.join(DATA_DIR, "gpt_guard.json"))
KST = timezone(timedelta(hours=9))


def format_sent_time(sent_at):
    """히스토리 라벨에 붙이는 전송 시각 (KST). 메시지마다 고정된 값이라 요청이 바뀌어도 캐시가 깨지지 않는다."""
    return sent_at.astimezone(KST).strftime("%m/%d %H:%M")


def format_current_time_marker():
    """현재 메시지 맨 앞에 봇이 자동으로 붙이는 현재 시각 줄 (KST).

    매 분 바뀌는 값이라 시스템 프롬프트(히스토리 앞)에 두면 히스토리 캐시가 깨져서, 매번 새로 바뀌는
    현재 메시지에 붙인다. 이 줄의 뜻은 시스템 프롬프트가 설명한다 (Gemini·LLM 봇과 같은 문구).
    """
    now = datetime.now(KST)
    return (
        f"[System time: {now.strftime('%Y-%m-%d (%a) %H:%M KST')} — this is the real current date, "
        f"so do not assume an earlier year than {now.year}]"
    )

# ================= 2. 데이터 관리자 =================
class DataManager:
    @staticmethod
    def load_json(filename, default_data):
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return default_data
        return default_data

    @staticmethod
    def save_json(filename, data):
        # 저장 도중 꺼져도 파일이 깨지지 않게 임시 파일에 쓰고 바꿔 끼운다
        tmp_path = f"{filename}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, filename)
        except Exception as e:
            print(f"[WARN] {filename} 저장 실패: {e}")


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
        DataManager.save_json(self.index_file, self.index)

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

    def render_blocks(self, message_id):
        """메시지에 딸린 문서·웹페이지 본문 (Gemini 봇과 같은 `Attached file "이름":` 형식)."""
        blocks = []
        for doc in self.get_docs_for_message(message_id):
            content = self.get_content(doc["id"])
            if content is None:
                continue
            source = "Webpage" if doc.get("type") == "webpage" else "Attached file"
            blocks.append(f'{source} "{doc.get("source_label", "")}":\n\n{trim_document_text(content)}')
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


def is_document_attachment(att):
    return (att.filename or "").lower().endswith(DOCUMENT_EXTENSIONS)


async def extract_attachment_text(att):
    """문서 첨부 본문. 확장자·크기를 먼저 보고, 읽을 수 있는 파일만 받는다."""
    if not is_document_attachment(att) or (att.size or 0) > MAX_DOC_SOURCE_BYTES:
        return None, None
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
    decoded = decode_text_bytes(raw_bytes)
    return trim_document_text(decoded), "text"

# ================= 3. 히스토리 =================
def clamp_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = HISTORY_LIMIT
    return max(MIN_HISTORY_LIMIT, min(MAX_HISTORY_LIMIT, limit))


def get_history_limit(config):
    return clamp_history_limit((config or {}).get("history_limit", HISTORY_LIMIT))


def clamp_image_history_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = IMAGE_HISTORY_LIMIT
    return max(MIN_IMAGE_HISTORY_LIMIT, min(MAX_IMAGE_HISTORY_LIMIT, limit))


def get_image_history_limit(config):
    return clamp_image_history_limit((config or {}).get("image_history_limit", IMAGE_HISTORY_LIMIT))


def clamp_history_block(value):
    try:
        block = int(value)
    except (TypeError, ValueError):
        block = HISTORY_BLOCK
    return max(MIN_HISTORY_BLOCK, min(MAX_HISTORY_BLOCK, block))


def get_history_block(config):
    return clamp_history_block((config or {}).get("history_block", HISTORY_BLOCK))


def select_history_window(
    message_ids,
    *,
    anchor_id,
    covered,
    base,
    block,
    media_counts,
    media_bytes,
    media_cap,
    media_bytes_cap,
):
    """블록 단위 히스토리 창의 시작 위치를 고른다. 반환: (시작 인덱스, 이유) — Gemini·LLM 봇과 같은 규칙

    message_ids: 히스토리 후보 ID (오래된 → 최신, 현재 메시지 제외)
    anchor_id: 이 채널의 현재 블록 시작 메시지 ID (봇 재시작 직후 등은 None)
    covered: 후보 목록이 anchor까지 거슬러 올라갔는지 (아니면 블록을 새로 시작)
    창 길이는 base ~ base + block - 1 사이를 오간다. 미디어 개수·용량이 상한을 넘으면
    블록을 일찍 교체해 최근 base개로 줄인다. 최근 base개만으로도 상한을 넘으면 교체해도 소용없고
    메시지마다 교체가 반복돼 캐시만 깨지므로 그때는 교체하지 않는다.
    이유: keep(블록 유지) | new(새 블록) | rollover(정기 교체) | media(미디어 때문에 조기 교체)
    """
    total = len(message_ids)
    if total == 0:
        return 0, "new"
    if anchor_id is not None and covered:
        start = bisect.bisect_left(message_ids, anchor_id)
        reason = "keep"
    else:
        start = max(0, total - base)
        reason = "new"
    if total - start < base and start > 0:
        start, reason = max(0, total - base), "new"
    window = total - start
    if window >= base + block:
        start += ((window - base) // block) * block
        reason = "rollover"
    if media_cap > 0:
        early_start = max(start, total - base)
        over_now = sum(media_counts[start:]) > media_cap or sum(media_bytes[start:]) > media_bytes_cap
        fits_after = (
            sum(media_counts[early_start:]) <= media_cap and sum(media_bytes[early_start:]) <= media_bytes_cap
        )
        if over_now and early_start > start and fits_after:
            start, reason = early_start, "media"
    return start, reason


def estimated_image_bytes(att):
    """다운로드 전 크기 추정: 큰 이미지는 IMAGE_MAX_BYTES 이하로 다시 저장된다."""
    return min(att.size or 0, IMAGE_MAX_BYTES)


def plan_history_images(msg, *, enabled):
    """히스토리 메시지에서 다시 보낼 이미지 첨부 (다운로드 전, 첨부 메타데이터만 본다). 봇이 만든 이미지도 포함한다."""
    if not enabled:
        return []
    return [
        att for att in msg.attachments
        if is_image_attachment(att) and 0 < (att.size or 0) <= IMAGE_SOURCE_MAX_BYTES
    ]


def fit_history_images_to_budget(image_plans, bytes_cap):
    """창 안 이미지 합계가 상한을 넘으면 최신부터 담고, 넘치는 오래된 이미지는 뺀다."""
    fitted = [[] for _ in image_plans]
    used = 0
    for index in range(len(image_plans) - 1, -1, -1):
        for att in image_plans[index]:
            size = estimated_image_bytes(att)
            if used + size > bytes_cap:
                continue
            used += size
            fitted[index].append(att)
    return fitted


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


def build_speaker_labels(speakers):
    """단톡방처럼 `표시명:`으로 쓸 사용자 라벨 (Gemini 봇 label_discord_user_contents와 같은 규칙).

    speakers: 대화 순서대로 (user_id, display_name). 같은 ID는 가장 최근 표시명으로 통일하고,
    표시명이 같은 서로 다른 사람은 처음 나온 순서대로 (1), (2)를 붙인다. 숫자 ID는 모델 입력에 넣지 않는다.
    """
    ordered_ids = []
    names = {}
    for user_id, display_name in speakers:
        if user_id not in names:
            ordered_ids.append(user_id)
        names[user_id] = (display_name or str(user_id)).strip()
    ids_by_name = {}
    for user_id in ordered_ids:
        ids_by_name.setdefault(names[user_id], []).append(user_id)
    labels = {}
    for name, user_ids in ids_by_name.items():
        for index, user_id in enumerate(user_ids, start=1):
            labels[user_id] = name if len(user_ids) == 1 else f"{name} ({index})"
    return labels


def strip_bot_name_prefix(text):
    if ":" in text[:20]:
        head, sep, rest = text.partition(": ")
        if sep and head in (get_bot_display_name(), get_bot_username(), "Bot", "Model"):
            return rest
    return text


def clean_response_text(text, speaker_labels):
    """모델이 시각 줄·봇 이름·대화 기록의 `표시명 (시각):`·`표시명:` 라벨을 흉내 내 답장 앞에 붙이면 지운다."""
    text = re.sub(r"^\s*\[System time: [^\]\n]+\]\s*", "", text or "")
    text = re.sub(r"^[^\n:]{1,60} \(\d{2}/\d{2} \d{2}:\d{2}\):[ \t]*\n?", "", text, count=1)
    bot_name = get_bot_display_name()
    if text.startswith(f"{bot_name}:"):
        text = text[len(bot_name) + 1:].lstrip()
    for label in speaker_labels.values():
        if text.startswith(f"{label}:"):
            text = text[len(label) + 1:].lstrip()
            break
    return text.strip()


def is_bot_notice(content):
    return (content or "").startswith(BOT_NOTICE_PREFIXES)


def mark_cache_breakpoint(history):
    """히스토리 마지막 사용자 턴에 캐시 지점을 둔다.

    현재 메시지는 맨 앞 시각 줄 때문에 다음 요청에서 같은 모양으로 다시 나오지 않으므로, 다음 요청이 읽을 수
    있는 캐시는 히스토리까지다. 자동 지점은 현재 메시지 끝에 잡혀 쓰기 요금만 내게 되므로 explicit 모드를 쓴다.
    """
    for item in reversed(history):
        if item["role"] == "user":
            item["content"][-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
            return

# ================= 4. 이미지 =================
OPENAI_IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
EXIF_ORIENTATION = 0x0112


class PreparedImage:
    """GPT·사전 검사에 그대로 넣는 이미지 (base64)."""

    __slots__ = ("media_type", "data", "size")

    def __init__(self, media_type, data, size):
        self.media_type = media_type
        self.data = data
        self.size = size

    def block(self):
        return {"type": "input_image", "image_url": self.data_url(), "detail": "auto"}

    def data_url(self):
        return f"data:{self.media_type};base64,{self.data}"


def prepare_image_bytes(raw):
    """원본을 GPT가 받는 이미지로 맞춘다. 반환 (bytes, media_type) 또는 None(못 읽음).

    JPEG/PNG/GIF/WebP이고 변 IMAGE_MAX_EDGE·IMAGE_MAX_BYTES 이하면 원본 그대로 둔다. 아니면(움직이는 이미지,
    회전 정보가 있는 사진, 큰 이미지, 다른 형식) 첫 프레임을 IMAGE_MAX_EDGE 안으로 줄여 JPEG로 다시 저장한다.
    같은 입력이면 늘 같은 바이트가 나와 히스토리로 다시 보낼 때도 캐시가 맞는다.
    """
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            media_type = OPENAI_IMAGE_TYPES.get((img.format or "").upper())
            animated = bool(getattr(img, "is_animated", False))
            rotated = img.getexif().get(EXIF_ORIENTATION, 1) not in (0, 1)
            if media_type and not animated and not rotated and max(img.size) <= IMAGE_MAX_EDGE and len(raw) <= IMAGE_MAX_BYTES:
                img.load()  # 깨진 파일은 여기서 걸러진다
                return raw, media_type
            img.seek(0)
            img.draft("RGB", (IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))  # 큰 JPEG는 줄여서 디코딩 (메모리 절약)
            frame = ImageOps.exif_transpose(img)
            if frame.mode in ("RGBA", "LA", "PA") or (frame.mode == "P" and "transparency" in frame.info):
                rgba = frame.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                frame = flat
            else:
                frame = frame.convert("RGB")
            frame.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE), Image.Resampling.LANCZOS)
            data = b""
            for quality in (90, 80, 70, 60):
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= IMAGE_MAX_BYTES:
                    break
            return data, "image/jpeg"
    except Exception as e:
        print(f"[IMAGE] 이미지 처리 실패: {type(e).__name__}: {e}")
        return None


class ImageCache:
    """첨부 ID → PreparedImage. 히스토리 이미지를 메시지마다 다시 받고 다시 줄이지 않게 메모리에 둔다."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self.items = OrderedDict()
        self.used = 0

    def _put(self, key, item):
        old = self.items.pop(key, None)
        self.used -= len(old.data) if old else 0
        self.items[key] = item
        self.used += len(item.data) if item else 0
        while self.used > self.max_bytes and len(self.items) > 1:
            _, evicted = self.items.popitem(last=False)
            self.used -= len(evicted.data) if evicted else 0

    async def get(self, att):
        if att.id in self.items:
            self.items.move_to_end(att.id)
            return self.items[att.id]
        if (att.size or 0) > IMAGE_SOURCE_MAX_BYTES:
            return None
        raw, _mime = await download_binary(att.url)
        if not raw:
            return None  # 받기 실패는 기억하지 않는다 (다음 메시지에서 다시 시도)
        prepared = await asyncio.to_thread(prepare_image_bytes, raw)
        item = None
        if prepared:
            data, media_type = prepared
            item = PreparedImage(media_type, base64.b64encode(data).decode("ascii"), len(data))
        self._put(att.id, item)  # 못 읽는 파일은 None으로 기억해 다시 처리하지 않는다
        return item


async def download_binary(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.read(), resp.headers.get("Content-Type")
    except Exception:
        pass
    return None, None


def is_image_attachment(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    filename = (getattr(attachment, "filename", None) or "").lower()
    return content_type.startswith("image/") or filename.endswith(IMAGE_ATTACHMENT_EXTENSIONS)

# ================= 5. 사전 검사 (OpenAI Moderation) =================
# OpenAI 이용 정책 위반으로 계정이 제재되지 않게, GPT에 보내기 전에 무료 OpenAI Moderation으로 거른다.
# 이 조직이 막히면 Claude 봇의 사전 검사도 같이 멈추므로 Claude 봇과 같은 기준을 쓴다.
# 정규식·키워드 규칙은 쓰지 않는다. 걸린 메시지는 🚫 반응만 달고(답장 없음) 히스토리에서도 뺀다.
#   항상 막음: sexual/minors(점수 GUARD_MINORS_THRESHOLD 이상이면 flagged가 아니어도), illicit/violent(무기·폭력 범죄 방법),
#             self-harm/instructions(자해 방법), 이미지의 sexual(이미지는 미성년 여부를 판정하지 못한다)
#   /guard로 끌 수 있음: 텍스트의 sexual (성인 대상 노골적 성적 내용) — 기본 켜짐
# 욕설·비하, 장난성 폭력, 자해 하소연(self-harm, self-harm/intent)은 막지 않는다.
OPENAI_MODERATION_URL = "https://api.openai.com/v1/moderations"
MODERATION_MODEL = "omni-moderation-latest"
GUARD_WATCHED = ("sexual", "sexual/minors", "illicit/violent", "self-harm/instructions")
GUARD_ALWAYS_BLOCK = ("sexual/minors", "illicit/violent", "self-harm/instructions", "sexual/image")
GUARD_MINORS_THRESHOLD = 0.2
MODERATION_CHUNK_CHARS = 6000  # 긴 글(문서·웹페이지 포함)은 잘라서 각각 검사
MODERATION_BATCH = 32  # 텍스트는 한 번에 여러 개 (결과도 하나씩), 이미지는 요청당 1장만 받는다
MODERATION_CONCURRENCY = 4
GUARD_MAX_ENTRIES = 5000
REACT_BLOCKED = "🚫"
REACT_GUARD_ERROR = "⚠️"
REACT_HISTORY_RESET = "🧹"
REFUSAL_RESET_STREAK = 2  # 연속으로 이만큼 거부되면 앞 대화가 거부를 부르는 것으로 보고 히스토리를 새로 시작


class GuardUnavailable(Exception):
    """검사를 끝내지 못함 → 검사 안 된 내용은 GPT에 보내지 않는다."""


def guard_reasons(entry, guard_sexual):
    """검사 기록으로 막을 이유 목록을 만든다. 빈 목록이면 통과."""
    if not entry:
        return []
    reasons = []
    if entry.get("r"):
        reasons.append("refused")
    flags = entry.get("f") or []
    for category in GUARD_ALWAYS_BLOCK:
        if category in flags:
            reasons.append(category)
    if "sexual/minors" not in reasons and (entry.get("m") or 0) >= GUARD_MINORS_THRESHOLD:
        reasons.append("sexual/minors")
    if guard_sexual and "sexual" in flags:
        reasons.append("sexual")
    return reasons


def guard_hash(msg, image_atts):
    """검사한 내용이 그대로인지 확인하는 값 (본문 + 딸린 문서·웹페이지 + 이미지 첨부). 수정되면 다시 검사한다."""
    docs = ",".join(f"{doc['id']}:{doc.get('size')}" for doc in bot.doc_cache.get_docs_for_message(msg.id))
    images = ",".join(str(att.id) for att in image_atts)
    raw = f"{msg.content or ''}\x00{docs}\x00{images}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class GuardStore:
    """메시지별 사전 검사 결과와 GPT 거부 기록. 내용은 저장하지 않고 걸린 분류만 남긴다.

    entries: {메시지 ID: {"c": 채널 ID, "h": guard_hash, "f": [걸린 분류], "m": sexual/minors 점수, "r": 1(거부됨)}}
    히스토리가 더는 볼 수 없는 오래된 메시지 기록은 prune으로 지운다.
    """

    def __init__(self, path):
        self.path = path
        data = DataManager.load_json(path, {})
        entries = data.get("entries") if isinstance(data, dict) else None
        self.entries = entries if isinstance(entries, dict) else {}
        self.dirty = False

    def get(self, msg_id, content_hash=None):
        entry = self.entries.get(str(msg_id))
        if entry is None:
            return None
        # 거부 기록은 내용이 수정돼도 유지한다 (다시 보내지 않는다)
        if content_hash is not None and entry.get("h") != content_hash and not entry.get("r"):
            return None
        return entry

    def put(self, msg_id, channel_id, content_hash, flags, minors):
        old = self.entries.get(str(msg_id)) or {}
        entry = {"c": str(channel_id), "h": content_hash, "f": sorted(set(flags)), "m": round(float(minors), 4)}
        if old.get("r"):
            entry["r"] = 1
        self.entries[str(msg_id)] = entry
        self.dirty = True

    def mark_refused(self, msg_id, channel_id):
        entry = self.entries.setdefault(str(msg_id), {"c": str(channel_id)})
        entry["r"] = 1
        self.dirty = True

    def is_excluded(self, msg_id, guard_sexual):
        return bool(guard_reasons(self.entries.get(str(msg_id)), guard_sexual))

    def prune(self, channel_id, min_id):
        """이 채널에서 min_id보다 오래된 메시지 기록을 지운다. 반환: 지운 개수"""
        channel_key = str(channel_id)
        stale = [key for key, entry in self.entries.items() if entry.get("c") == channel_key and int(key) < min_id]
        for key in stale:
            del self.entries[key]
        if len(self.entries) > GUARD_MAX_ENTRIES:
            for key in sorted(self.entries, key=int)[:len(self.entries) - GUARD_MAX_ENTRIES]:
                del self.entries[key]
                stale.append(key)
        if stale:
            self.dirty = True
        return len(stale)

    def clear_channel(self, channel_id):
        channel_key = str(channel_id)
        for key in [key for key, entry in self.entries.items() if entry.get("c") == channel_key]:
            del self.entries[key]
        self.dirty = True

    def save(self):
        if self.dirty:
            DataManager.save_json(self.path, {"version": 1, "entries": self.entries})
            self.dirty = False


def summarize_moderation(result, *, image):
    """Moderation 결과 하나 → (걸린 분류 집합, sexual/minors 점수). 이미지의 sexual은 sexual/image로 따로 센다."""
    categories = result.get("categories") or {}
    scores = result.get("category_scores") or {}
    flags = {category for category in GUARD_WATCHED if categories.get(category)}
    if image:
        flags = {"sexual/image" if category == "sexual" else category for category in flags}
        return flags, 0.0
    return flags, float(scores.get("sexual/minors") or 0.0)


async def _moderation_request(inputs):
    """OpenAI Moderation 호출. 429·5xx·네트워크 오류는 두 번까지 다시 시도한다."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    body = {"model": MODERATION_MODEL, "input": inputs}
    expected = len(inputs) if all(isinstance(item, str) for item in inputs) else 1
    last_error = None
    for attempt in range(3):
        delay = 1.5 * (attempt + 1)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OPENAI_MODERATION_URL, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        results = json.loads(text).get("results") or []
                        if len(results) != expected:
                            raise GuardUnavailable(f"결과 개수가 맞지 않음 ({len(results)}/{expected})")
                        return results
                    last_error = f"HTTP {resp.status}: {text[:200]}"
                    if resp.status != 429 and resp.status < 500:
                        break
                    try:
                        delay = max(delay, min(float(resp.headers.get("retry-after") or 0), 5.0))
                    except ValueError:
                        pass
        except GuardUnavailable:
            raise
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt < 2:
            await asyncio.sleep(delay)
    raise GuardUnavailable(last_error or "알 수 없는 오류")


async def moderate(items, *, partial=False):
    """items: [(key, [텍스트...], [이미지 data URL...])] → {key: (걸린 분류 집합, sexual/minors 점수)}

    텍스트는 key마다 따로(긴 글은 잘라서 한 번에), 이미지는 한 장씩 검사한다. key 하나의 실패가 다른 key에 번지지 않는다.
    partial=False: 하나라도 검사하지 못하면 GuardUnavailable (검사 안 된 내용은 보내지 않는다).
    partial=True: (검사한 key의 결과, {검사 못 한 key: 오류})를 돌려준다.
    """
    verdicts = {key: (set(), 0.0) for key, _, _ in items}
    jobs = []
    for key, texts, images in items:
        chunks = []
        for text in texts:
            text = (text or "").strip()
            chunks.extend(text[start:start + MODERATION_CHUNK_CHARS] for start in range(0, len(text), MODERATION_CHUNK_CHARS))
        for start in range(0, len(chunks), MODERATION_BATCH):
            jobs.append((key, chunks[start:start + MODERATION_BATCH], False))
        for url in images:
            jobs.append((key, [{"type": "image_url", "image_url": {"url": url}}], True))
    semaphore = asyncio.Semaphore(MODERATION_CONCURRENCY)

    async def run(inputs):
        async with semaphore:
            return await _moderation_request(inputs)

    outcomes = await asyncio.gather(*(run(inputs) for _, inputs, _ in jobs), return_exceptions=True)
    failed = {}
    for (key, _, is_image), outcome in zip(jobs, outcomes):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, GuardUnavailable):
                outcome = GuardUnavailable(f"{type(outcome).__name__}: {outcome}")
            failed.setdefault(key, outcome)
            continue
        for result in outcome:
            flags, minors = summarize_moderation(result, image=is_image)
            old_flags, old_minors = verdicts[key]
            verdicts[key] = (old_flags | flags, max(old_minors, minors))
    if partial:
        return {key: value for key, value in verdicts.items() if key not in failed}, failed
    if failed:
        raise next(iter(failed.values()))
    return verdicts


def hash_user_id(user_id):
    """safety_identifier 값. OpenAI가 남용 탐지에 쓰는 값이라 디스코드 ID 대신 해시를 보낸다."""
    return hashlib.sha256(f"nr-discord-gpt:{user_id}".encode("utf-8")).hexdigest()

# ================= 6. 웹 검색 / 링크 읽기 =================
# 검색 방법(검색어·언어·출처·기간·깊이·횟수)은 모델이 정한다. 설명에는 각 옵션이 하는 일과 비용만 적는다.
TAVILY_TOPICS = ("general", "news")
TAVILY_TIME_RANGES = ("day", "week", "month", "year")
TAVILY_SEARCH_DEPTHS = ("basic", "advanced", "fast", "ultra-fast")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TAVILY_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web (Tavily). Each result has a title, URL, published date when known, and relevant "
        f"text from the page. You decide how to search. Up to {WEB_SEARCH_MAX_CALLS} searches per message."
    ),
    "strict": False,  # 선택 옵션이 많아 strict 스키마(전부 required)로 만들지 않는다
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "topic": {
                "type": "string",
                "enum": list(TAVILY_TOPICS),
                "description": "general (default): any web page. news: news articles.",
            },
            "time_range": {
                "type": "string",
                "enum": list(TAVILY_TIME_RANGES),
                "description": (
                    "Only results published within this period before now. "
                    "Cannot be combined with start_date/end_date."
                ),
            },
            "start_date": {
                "type": "string",
                "description": "Only results published on or after this date (YYYY-MM-DD).",
            },
            "end_date": {
                "type": "string",
                "description": "Only results published on or before this date (YYYY-MM-DD).",
            },
            "search_depth": {
                "type": "string",
                "enum": list(TAVILY_SEARCH_DEPTHS),
                "description": (
                    "basic (default): balanced relevance and speed. advanced: highest relevance, slower, "
                    "costs 2 credits instead of 1. fast: lower latency. ultra-fast: lowest latency, "
                    "one summary per page."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": TAVILY_MAX_RESULTS_CAP,
                "description": f"Number of results (default {TAVILY_MAX_RESULTS}).",
            },
            "country": {
                "type": "string",
                "description": (
                    "Boost results from this country, as a lowercase English country name such as "
                    "'south korea', 'united states', 'japan'. Applies to topic general."
                ),
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only return results from these domains.",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Never return results from these domains.",
            },
        },
        "required": ["query"],
    },
}

# Tavily를 못 쓰거나 /search로 고르면 쓰는 OpenAI hosted 도구. 미리 읽지 못한 링크가 있을 때도 이것으로 직접 연다.
SEOUL_LOCATION = {"type": "approximate", "city": "Seoul", "region": "Seoul", "country": "KR", "timezone": "Asia/Seoul"}
NATIVE_SEARCH_TOOL = {"type": "web_search", "user_location": SEOUL_LOCATION}

TAVILY_STATE = {"down_until": 0.0, "reason": None}


class TavilyOutage(Exception):
    """키·크레딧·서버 문제처럼 모델이 옵션을 고쳐도 풀리지 않는 Tavily 실패."""


def is_tavily_outage(status):
    # 400은 모델이 고른 옵션 문제라 모델에게 돌려준다. 401 키, 429 속도, 432/433 크레딧·한도, 5xx·네트워크는 장애로 본다.
    return status is None or status in (401, 403, 429, 432, 433) or status >= 500


def tavily_available():
    return bool(TAVILY_API_KEY) and time.time() >= TAVILY_STATE["down_until"]


def mark_tavily_down(reason):
    TAVILY_STATE["down_until"] = time.time() + TAVILY_OUTAGE_COOLDOWN
    TAVILY_STATE["reason"] = str(reason)[:200]
    print(f"[SEARCH] Tavily 사용 불가 → {TAVILY_OUTAGE_COOLDOWN // 60}분 동안 OpenAI 기본 검색 사용 ({reason})")


async def _tavily_post(url, body, timeout_seconds):
    """Tavily API POST. 반환: (data, 오류 문자열, HTTP 상태 — 네트워크 오류면 None)"""
    if not TAVILY_API_KEY:
        return None, "TAVILY_API_KEY가 설정되지 않았습니다.", None
    headers = {"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return None, f"HTTP {resp.status}: {text[:300]}", resp.status
                return json.loads(text), None, resp.status
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", None


def tavily_search_options(options):
    """모델이 고른 검색 옵션 중 형식이 맞는 것만 Tavily 요청 필드로 옮긴다.

    옵션 조합은 판단하지 않는다. 잘못된 조합(예: time_range + start_date)이나 없는 국가명은
    Tavily가 400으로 이유를 돌려주고, 그 문구가 모델에게 그대로 가서 모델이 고쳐 다시 검색할 수 있다.
    """
    options = options or {}
    picked = {}
    if options.get("topic") in TAVILY_TOPICS:
        picked["topic"] = options["topic"]
    if options.get("time_range") in TAVILY_TIME_RANGES:
        picked["time_range"] = options["time_range"]
    for key in ("start_date", "end_date"):
        value = str(options.get(key) or "").strip()
        if ISO_DATE_RE.match(value):
            picked[key] = value
    if options.get("search_depth") in TAVILY_SEARCH_DEPTHS:
        picked["search_depth"] = options["search_depth"]
    try:
        max_results = int(options.get("max_results") or TAVILY_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = TAVILY_MAX_RESULTS
    picked["max_results"] = min(max(max_results, 1), TAVILY_MAX_RESULTS_CAP)
    country = str(options.get("country") or "").strip().lower()
    if country:
        picked["country"] = country
    for key in ("include_domains", "exclude_domains"):
        domains = options.get(key)
        if isinstance(domains, str):
            domains = [domains]
        if isinstance(domains, list):
            cleaned = [str(d).strip() for d in domains if str(d).strip()][:20]
            if cleaned:
                picked[key] = cleaned
    return picked


async def tavily_search(query, options=None):
    """Tavily 검색. 반환: {"ok", "results", "credits", "error", "status", "options"(실제로 보낸 검색 옵션)}"""
    picked = tavily_search_options(options)
    body = {"query": query, **picked, "include_published_date": True, "include_usage": True}
    data, error, status = await _tavily_post(TAVILY_SEARCH_URL, body, timeout_seconds=40)
    if error:
        return {"ok": False, "results": [], "credits": None, "error": error, "status": status, "options": picked}
    results = [
        {
            "title": item.get("title") or "",
            "url": item["url"],
            "content": (item.get("content") or "")[:TAVILY_RESULT_CHARS],
            "published_date": item.get("published_date"),
        }
        for item in data.get("results") or []
        if item.get("url")
    ]
    credits = (data.get("usage") or {}).get("credits")
    return {"ok": True, "results": results, "credits": credits, "error": None, "status": status, "options": picked}


def format_search_results(query, outcome):
    """모델에게 넘길 검색 결과 텍스트. 실패도 숨기지 않고 그대로 알린다.

    검색 시각을 같이 적는다. 모델이 학습 시점을 현재로 착각해 최신 결과를 '미래 날짜라 이상하다'며
    버리는 일이 실측됐다(DeepSeek V4).
    """
    if not outcome.get("ok"):
        return (
            f"[web_search 실패] query={query!r}: {outcome.get('error')}\n"
            "옵션을 고쳐 다시 검색해도 된다. 검색 없이 답한다면 최신 정보가 필요한 부분은 확인이 필요하다고 밝혀라."
        )
    if not outcome.get("results"):
        return f"[web_search 결과 없음] query={query!r}"
    searched_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"[web_search 결과] query={query!r} (검색 시각: {searched_at})"]
    for i, item in enumerate(outcome["results"], start=1):
        date = f" ({item['published_date']})" if item.get("published_date") else ""
        lines.append(f"{i}. {item['title']}{date}\n   URL: {item['url']}\n   {item['content']}")
    return "\n".join(lines)


def make_web_search_handler(max_calls=WEB_SEARCH_MAX_CALLS, sources=None):
    """메시지 하나 동안 쓰는 Tavily web_search 실행기. 반환값: (도구 결과 텍스트, 오류 여부)

    호출 횟수를 max_calls로 제한한다. sources에 리스트를 주면 검색 결과(제목·URL)를 모아 답장 끝 출처 목록에 쓴다.
    Tavily 장애(키·크레딧·서버)는 TavilyOutage로 올려 OpenAI 기본 검색으로 다시 시도하게 한다.
    """
    state = {"calls": 0}

    async def handler(args):
        args = args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return "[web_search 실패] 검색어가 비어 있습니다.", True
        if state["calls"] >= max_calls:
            return f"[web_search 중단] 이번 메시지의 검색 한도({max_calls}회)에 도달했습니다. 지금까지 찾은 결과로 답하세요.", True
        state["calls"] += 1
        outcome = await tavily_search(query, args)
        count = len(outcome.get("results") or [])
        detail = f"credits={outcome.get('credits')}" if outcome.get("ok") else f"error={outcome.get('error')}"
        print(
            f"[SEARCH] #{state['calls']} query={query!r} opts={outcome.get('options')} "
            f"ok={outcome.get('ok')} results={count} {detail}"
        )
        if not outcome.get("ok") and is_tavily_outage(outcome.get("status")):
            raise TavilyOutage(outcome.get("error"))
        if sources is not None:
            sources.extend(outcome.get("results") or [])
        return format_search_results(query, outcome), not outcome.get("ok")

    return handler


def format_search_sources(sources, response_text, limit=5):
    """검색 결과 출처를 Gemini 봇과 같은 `**출처:**` 번호 목록으로. 본문에 이미 있는 URL·중복은 뺀다."""
    seen = set()
    lines = []
    for item in sources or []:
        url = (item.get("url") or "").strip()
        if not url or url in seen or url in (response_text or ""):
            continue
        seen.add(url)
        title = urlsplit(url).netloc.removeprefix("www.") or url
        safe_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        safe_url = url.replace("\\", "%5C").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        lines.append(f"{len(lines) + 1}. [{safe_title}]({safe_url})")
        if len(lines) >= limit:
            break
    return "**출처:**\n" + "\n".join(lines) if lines else ""


async def tavily_extract(urls):
    """Tavily Extract로 URL 본문을 읽는다. 반환: ({url: 본문}, {url: 오류})"""
    urls = [u for u in urls if u][:20]
    if not urls:
        return {}, {}
    data, error, status = await _tavily_post(
        TAVILY_EXTRACT_URL,
        {"urls": urls, "extract_depth": "basic", "format": "markdown", "include_usage": True},
        timeout_seconds=60,
    )
    if error:
        print(f"[WEB] Tavily Extract 실패 urls={len(urls)} error={error}")
        if is_tavily_outage(status):
            mark_tavily_down(error)
        return {}, {u: error for u in urls}
    contents = {
        item["url"]: item["raw_content"]
        for item in data.get("results") or []
        if item.get("url") and (item.get("raw_content") or "").strip()
    }
    failed = {
        item["url"]: item.get("error") or "추출 실패"
        for item in data.get("failed_results") or []
        if item.get("url")
    }
    credits = (data.get("usage") or {}).get("credits")
    print(f"[WEB] Tavily Extract urls={len(urls)} ok={len(contents)} failed={len(failed)} credits={credits}")
    return contents, failed


async def is_public_url(url):
    """봇이 직접 열어도 되는 주소인지. 사설·루프백·Tailscale(100.64/10) 같은 내부 주소는 열지 않는다."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except (OSError, ValueError):
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return bool(infos)


def extract_page_text(html):
    """HTML 본문 텍스트. 본문 영역(main, 없으면 article)을 먼저 쓴다.

    페이지 전체를 읽으면 위키백과처럼 메뉴·언어 목록이 앞을 채워 MAX_DOC_CHARS로 자를 때 본문이 잘려 나갔다 (실측).
    """
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(["script", "style", "nav", "footer", "iframe", "header", "aside", "noscript", "form", "svg"]):
        tag.extract()

    def text_of(node):
        text = node.get_text(separator='\n')
        return '\n'.join([line.strip() for line in text.splitlines() if line.strip()])

    for node in (soup.find("main"), soup.find("article"), soup.body, soup):
        if node is None:
            continue
        clean_text = text_of(node)
        if len(clean_text) >= MIN_SCRAPED_CHARS:
            return clean_text
    return None


async def scrap_webpage(url):
    """링크 본문을 직접 읽는다. 내부 주소로 가는 링크(리디렉션 포함)는 열지 않는다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }
    current = url
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for _ in range(4):
                if not await is_public_url(current):
                    print(f"[WEB] 내부·잘못된 주소라 직접 읽지 않음: {current}")
                    return None
                async with session.get(current, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        current = urljoin(current, resp.headers["Location"])
                        continue
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    if resp.status != 200 or ("html" not in content_type and "text" not in content_type):
                        return None
                    # content.read(n)는 버퍼에 있는 만큼(첫 조각)만 돌려줘서, 상한까지 조각을 모은다
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(65536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_SCRAPE_BYTES:
                            break
                    html = b"".join(chunks).decode(resp.charset or "utf-8", errors="ignore")
                    return extract_page_text(html)
    except Exception as e:
        print(f"[WARN] URL 읽기 실패: {e}")
    return None


URL_PATTERN = re.compile(r"https?://[^\s<>]+")


def find_urls(text):
    urls = []
    for raw in URL_PATTERN.findall(text or ""):
        url = raw.rstrip(".,!?;:'\"")
        if url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        if url not in urls:
            urls.append(url)
    return urls[:MAX_LINKS_PER_MESSAGE]


async def read_links(message, user_text):
    """현재 메시지의 링크 본문을 읽어 user_text 끝에 붙인다. 반환: (user_text, 못 읽은 URL 목록)

    직접 읽기 → (PDF·실패한 링크) Tavily Extract → 그래도 못 읽은 링크는 OpenAI 기본 검색으로 GPT가 직접 열게 한다.
    """
    unresolved = []
    for url in find_urls(message.content):
        if urlsplit(url).path.lower().endswith(".pdf"):
            unresolved.append(url)
            continue
        web_content = await scrap_webpage(url)
        if web_content:
            doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
            print(f"[WEB] 웹사이트 내용 읽음: {url} ({doc_id})")
            user_text += f'\n\nWebpage "{url}":\n\n{trim_document_text(web_content)}'
        else:
            unresolved.append(url)
    unread = []
    if unresolved:
        extracted, extract_failed = {}, {}
        if tavily_available():
            extracted, extract_failed = await tavily_extract(unresolved)
        extracted_by_url = {u.rstrip("/"): text for u, text in extracted.items()}
        for url in unresolved:
            web_content = extracted.get(url) or extracted_by_url.get(url.rstrip("/"))
            if web_content:
                doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
                print(f"[WEB] Tavily Extract로 읽음: {url} ({doc_id})")
                user_text += f'\n\nWebpage "{url}":\n\n{trim_document_text(web_content)}'
            else:
                print(f"[WEB] 링크 미리 읽기 실패 → OpenAI 기본 검색 사용: {url} ({extract_failed.get(url) or 'Tavily 사용 안 함'})")
                unread.append(url)
                user_text += (
                    f"\n\n(System: 이 링크의 본문을 미리 읽지 못했습니다: {url} — web_search 도구로 직접 열어 보고, "
                    "그래도 못 읽으면 내용을 추측하지 말 것)"
                )
    return user_text, unread

# ================= 7. GPT 호출 =================
@dataclass
class ReplyResult:
    text: str
    stop_reason: Optional[str]
    backend: str
    images: list = field(default_factory=list)  # 생성된 이미지 bytes
    image_blocked: bool = False
    refusal_explanation: Optional[str] = None


class ResponseFailed(Exception):
    """OpenAI가 응답을 끝내지 못함 (response.failed, 또는 정책 차단 API 오류)."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


class UsageTally:
    """토큰·비용 합계 (요청 하나, 또는 봇 시작 후 전체)."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0  # 캐시 읽기·쓰기 포함
        self.cache_read = 0
        self.cache_write = 0
        self.output_tokens = 0
        self.native_searches = 0
        self.images = 0
        self.cost = 0.0

    @property
    def prompt_tokens(self):
        return self.input_tokens

    def add_response(self, model, response):
        def value(obj, name):
            return int(getattr(obj, name, 0) or 0) if obj is not None else 0

        usage = response.usage
        self.calls += 1
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            total_input = value(usage, "input_tokens")
            read = value(details, "cached_tokens")
            write = value(details, "cache_write_tokens")
            output = value(usage, "output_tokens")
            price_in, price_out, price_read, price_write = model["price"]
            self.input_tokens += total_input
            self.cache_read += read
            self.cache_write += write
            self.output_tokens += output
            self.cost += (
                (total_input - read - write) * price_in + read * price_read + write * price_write + output * price_out
            ) / 1_000_000
        tool_usage = getattr(response, "tool_usage", None)
        searches = value(getattr(tool_usage, "web_search", None), "num_requests")
        self.native_searches += searches
        self.cost += searches * NATIVE_SEARCH_PRICE
        image_usage = getattr(tool_usage, "image_gen", None)
        if image_usage is not None:
            image_details = getattr(image_usage, "input_tokens_details", None)
            text_price, image_price, output_price = IMAGE_TOKEN_PRICE
            self.cost += (
                value(image_details, "text_tokens") * text_price
                + value(image_details, "image_tokens") * image_price
                + value(image_usage, "output_tokens") * output_price
            ) / 1_000_000

    def merge(self, other):
        for name in ("calls", "input_tokens", "cache_read", "cache_write", "output_tokens",
                     "native_searches", "images", "cost"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def describe(self):
        cache_rate = f" ({self.cache_read * 100 // self.prompt_tokens}%)" if self.prompt_tokens else ""
        extra = ""
        if self.native_searches:
            extra += f", OpenAI 검색 {self.native_searches}회"
        if self.images:
            extra += f", 이미지 {self.images}장"
        return (
            f"In: {self.prompt_tokens}, Cached: {self.cache_read}{cache_rate}, Write: {self.cache_write}, "
            f"Out: {self.output_tokens}, calls={self.calls}{extra}, ~${self.cost:.5f}"
        )


async def call_openai(**request):
    # 이미지·사고 시간이 길어 스트리밍으로 받는다 (긴 요청의 연결 끊김 방지). 결과는 한 번에 쓴다.
    try:
        final = None
        # 끝 이벤트를 받아도 스트림을 끝까지 읽는다 (중간에 빠져나가면 연결 정리 중 오류가 난다)
        async with await bot.openai.responses.create(stream=True, **request) as stream:
            async for event in stream:
                if event.type in ("response.completed", "response.incomplete", "response.failed"):
                    final = event.response
                elif event.type == "error":
                    raise ResponseFailed(event.code, event.message)
    except openai.APIError as e:
        if getattr(e, "code", None) == "moderation_blocked":
            raise ResponseFailed("moderation_blocked", e.message) from e
        raise
    if final is None:
        raise RuntimeError("OpenAI 스트림이 응답 없이 끝났습니다.")
    return final


def build_image_tool(config):
    image_model = IMAGE_MODELS.get(config.get("image_model")) or IMAGE_MODELS[DEFAULT_IMAGE_MODEL_KEY]
    return {
        "type": "image_generation",
        "model": image_model["id"],
        "quality": config.get("image_quality") or DEFAULT_IMAGE_QUALITY,
        "size": config.get("image_size") or DEFAULT_IMAGE_SIZE,
        "output_format": "png",
    }


async def run_gpt(model, messages, *, effort, user_hash, cache_key, backend, image_tool, tally, sources):
    """GPT 호출 + 도구 루프. backend: tavily(Tavily 검색 도구) | native(OpenAI 기본 검색)

    store=False라 도구 왕복은 응답의 항목(암호화된 사고 포함)을 그대로 다시 보낸다. 거부되면 중간 글도 버린다.
    """
    search_tool = TAVILY_SEARCH_TOOL if backend == "tavily" else NATIVE_SEARCH_TOOL
    search_handler = make_web_search_handler(sources=sources) if backend == "tavily" else None
    convo = [DEVELOPER_ITEM] + list(messages)
    images = []
    image_blocked = False
    cited_urls = []
    tool_rounds = 0
    while True:
        images_left = max(0, IMAGE_MAX_PER_MESSAGE - len(images))
        hosted_calls = images_left + (WEB_SEARCH_MAX_CALLS if backend == "native" else 0)
        request = {
            "model": model["id"],
            "input": convo,
            "tools": ([image_tool] if images_left else []) + [search_tool],
            "store": False,
            "max_output_tokens": MAX_OUTPUT_TOKENS_DEEP if effort in ("xhigh", "max") else MAX_OUTPUT_TOKENS,
            "prompt_cache_options": {"mode": "explicit"},
            "prompt_cache_key": cache_key,
            "safety_identifier": user_hash,
        }
        if hosted_calls:
            request["max_tool_calls"] = hosted_calls
        if effort:
            request["reasoning"] = {"effort": effort}
        if backend == "native":
            request["include"] = ["web_search_call.action.sources"]
        if tool_rounds >= WEB_SEARCH_MAX_ROUNDS:
            request["tool_choice"] = "none"
        try:
            response = await call_openai(**request)
        except ResponseFailed as e:
            if e.code != "moderation_blocked":
                raise
            print(f"[IMAGE] 안전 필터 차단: {e}")
            return ReplyResult(text="", stop_reason="moderation_blocked", backend=backend, images=images, image_blocked=True)
        tally.add_response(model, response)
        if response.status == "failed":
            error = response.error
            code = getattr(error, "code", None)
            if code == "moderation_blocked":
                return ReplyResult(text="", stop_reason=code, backend=backend, images=images, image_blocked=True)
            raise ResponseFailed(code, getattr(error, "message", "알 수 없는 오류"))

        answer_parts = []
        refusal = None
        function_calls = []
        for item in response.output:
            if item.type == "function_call":
                function_calls.append(item)
            elif item.type == "image_generation_call":
                if item.result:
                    images.append(base64.b64decode(item.result))
                    tally.images += 1
                else:
                    error = getattr(item, "error", None)
                    code = getattr(error, "code", None)
                    image_blocked = image_blocked or code == "moderation_blocked"
                    print(f"[IMAGE] 생성 실패: status={item.status} code={code} message={getattr(error, 'message', None)}")
            elif item.type == "web_search_call":
                action = getattr(item, "action", None)
                sources.extend(
                    {"url": source.url} for source in getattr(action, "sources", None) or [] if getattr(source, "url", None)
                )
            elif item.type == "message":
                for part in item.content:
                    if part.type == "refusal":
                        refusal = part.refusal
                    elif part.type == "output_text":
                        answer_parts.append(part.text)
                        for annotation in part.annotations or []:
                            if annotation.type == "url_citation" and annotation.url not in cited_urls:
                                cited_urls.append(annotation.url)
        if refusal and not images:
            return ReplyResult(text="", stop_reason="refusal", backend=backend, refusal_explanation=refusal)

        if function_calls and response.status == "completed":
            tool_rounds += 1
            convo.extend(item.to_dict() for item in response.output)
            for call in function_calls:
                if call.name == "web_search" and search_handler is not None:
                    try:
                        args = json.loads(call.arguments or "{}")
                    except ValueError:
                        args = {}
                    content, _is_error = await search_handler(args)
                else:
                    content = f"알 수 없는 도구: {call.name}"
                convo.append({"type": "function_call_output", "call_id": call.call_id, "output": content})
            continue

        if cited_urls:
            # OpenAI 기본 검색: 참고만 한 페이지는 빼고 답에 인용된 출처만 남긴다
            sources[:] = [{"url": url} for url in cited_urls]
        incomplete = getattr(response.incomplete_details, "reason", None) if response.incomplete_details else None
        return ReplyResult(
            text="".join(answer_parts).strip(),
            stop_reason=incomplete or response.status,
            backend=backend,
            images=images,
            image_blocked=image_blocked,
        )


async def generate_reply(model, messages, *, effort, user_hash, cache_key, backend, image_tool, tally, sources):
    """답을 만든다. Tavily를 쓰다 장애가 나면 OpenAI 기본 검색으로 처음부터 다시 만든다."""
    try:
        return await run_gpt(
            model, messages, effort=effort, user_hash=user_hash, cache_key=cache_key, backend=backend,
            image_tool=image_tool, tally=tally, sources=sources,
        )
    except TavilyOutage as e:
        mark_tavily_down(e)
        sources.clear()
        return await run_gpt(
            model, messages, effort=effort, user_hash=user_hash, cache_key=cache_key, backend="native",
            image_tool=image_tool, tally=tally, sources=sources,
        )

# ================= 8. 봇 =================
class GPTBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            # 모델이 @everyone·역할 멘션을 따라 써도 실제로 알림이 가지 않게 한다
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        self.openai = openai.AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=600.0, max_retries=3)
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}
        self.history_anchors = {}  # 채널별 현재 히스토리 블록의 시작 메시지 ID (재시작하면 새 블록부터)
        self.refusal_streaks = {}  # 채널별 연속 거부 횟수
        self.doc_cache = DocumentCache(DOC_CACHE_DIR)
        self.guard = GuardStore(GUARD_FILE)
        self.images = ImageCache(IMAGE_CACHE_BYTES)
        self.stats = UsageTally()  # 봇 시작 후 누적
        self.counters = {"replies": 0, "blocked": 0, "refused": 0, "guard_errors": 0}

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ GPT Bot 준비 완료")

    def get_config(self, guild_id):
        gid = str(guild_id)
        config = self.server_configs.setdefault(gid, {"channel_id": None, "reset_time": None})
        config.setdefault("model", DEFAULT_MODEL_KEY)
        config.setdefault("effort", None)
        config.setdefault("search_mode", DEFAULT_SEARCH_MODE)
        config.setdefault("image_model", DEFAULT_IMAGE_MODEL_KEY)
        config.setdefault("image_quality", DEFAULT_IMAGE_QUALITY)
        config.setdefault("image_size", DEFAULT_IMAGE_SIZE)
        config.setdefault("history_limit", HISTORY_LIMIT)
        config.setdefault("history_block", HISTORY_BLOCK)
        config.setdefault("image_history_limit", IMAGE_HISTORY_LIMIT)
        config.setdefault("guard_sexual", True)
        return config

    def get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)


bot = GPTBot()


def get_model(config):
    return MODELS.get(config.get("model")) or MODELS[DEFAULT_MODEL_KEY]


def get_effort(config):
    effort = config.get("effort")
    return effort if effort in get_model(config)["efforts"] else None


def describe_effort(config):
    effort = get_effort(config)
    return f"`{effort}`" if effort else "모델 기본값"


def pick_search_backend(config, unread_urls):
    """미리 읽지 못한 링크가 있으면 직접 열 수 있는 OpenAI 기본 검색을 쓴다."""
    if unread_urls or config.get("search_mode") == "openai" or not tavily_available():
        return "native"
    return "tavily"


def describe_search(config):
    if config.get("search_mode") == "openai":
        return "OpenAI 기본 검색 (1회 $0.01)"
    if not TAVILY_API_KEY:
        return "OpenAI 기본 검색 (TAVILY_API_KEY 없음, 1회 $0.01)"
    if not tavily_available():
        until = datetime.fromtimestamp(TAVILY_STATE["down_until"], KST).strftime("%H:%M")
        return f"OpenAI 기본 검색으로 대신하는 중 (~{until} KST, Tavily: {TAVILY_STATE['reason']})"
    return "Tavily (모델이 필요할 때 검색)"


def describe_image_settings(config):
    image_model = IMAGE_MODELS.get(config.get("image_model")) or IMAGE_MODELS[DEFAULT_IMAGE_MODEL_KEY]
    return f"**{image_model['name']}** · 품질 `{config.get('image_quality')}` · 크기 `{config.get('image_size')}`"


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
        if split_index == -1:
            split_index = 1900
        chunks.append(content[:split_index])
        content = content[split_index:].lstrip()

    for chunk in chunks:
        await channel.send(chunk)
        await asyncio.sleep(0.5)


async def add_reaction(message, emoji):
    try:
        await message.add_reaction(emoji)
    except Exception as e:
        print(f"[WARN] 반응 달기 실패 ({emoji}): {e}")


async def send_images(channel, images):
    """생성한 이미지를 올린다. 서버 업로드 한도를 넘으면 JPEG로 줄인다."""
    limit = getattr(channel.guild, "filesize_limit", 10 * 1024 * 1024)
    files = []
    for index, data in enumerate(images, start=1):
        extension = "png"
        if len(data) > limit:
            prepared = await asyncio.to_thread(prepare_image_bytes, data)
            if not prepared:
                print(f"[IMAGE] 업로드 한도를 넘고 줄이지도 못함: {len(data)}B")
                continue
            data, extension = prepared[0], "jpg"
        files.append(discord.File(io.BytesIO(data), filename=f"gpt_image_{index}.{extension}"))
    if files:
        await channel.send(files=files)


async def read_current_attachments(message, user_text):
    """현재 메시지 첨부. 반환: (user_text, 보낼 이미지, 검사할 이미지)

    이미지는 전부 검사하고, 요청 크기 한도 안에서만 보낸다. 문서는 본문을 user_text에 붙인다.
    """
    image_atts = [att for att in message.attachments if is_image_attachment(att)]
    prepared = await asyncio.gather(*(bot.images.get(att) for att in image_atts))
    images, screen_images = [], []
    used = 0
    for att, item in zip(image_atts, prepared):
        if item is None:
            user_text += f"\n(User attached an image that could not be processed: {att.filename})"
            continue
        screen_images.append(item)
        if used + item.size > CURRENT_MEDIA_BYTES_CAP:
            user_text += f"\n(User attached another image, skipped because the message is too large: {att.filename})"
            continue
        used += item.size
        images.append(item)
        print(f"[ATT] 이미지 준비: {att.filename} {item.media_type} {item.size}B")
    for att in message.attachments:
        if is_image_attachment(att):
            continue
        content_type = (att.content_type or "").lower()
        if content_type.startswith("video/"):
            user_text += f"\n(User attached a video: {att.filename})"
            continue
        if content_type.startswith("audio/"):
            user_text += f"\n(User attached an audio file: {att.filename})"
            continue
        extracted_text, doc_type = await extract_attachment_text(att)
        if extracted_text:
            doc_id = bot.doc_cache.store_document("text_file", att.filename, extracted_text, message.id, message.channel.id)
            print(f"[CACHE] {doc_type or 'text'} file 저장: {doc_id} ({att.filename})")
            user_text += f'\n\nAttached file "{att.filename}":\n\n{extracted_text}'
        else:
            user_text += f"\n(User attached a file that could not be read: {att.filename})"
    return user_text, images, screen_images


async def ensure_history_documents(msg):
    """히스토리 메시지의 문서 첨부를 캐시에 넣어 둔다 (본문은 캐시 기준으로 넣어 요청마다 같은 모양이 되게)."""
    for att in msg.attachments:
        if is_image_attachment(att) or not is_document_attachment(att):
            continue
        if bot.doc_cache.has_source(msg.id, "text_file", att.filename):
            continue
        extracted_text, _doc_type = await extract_attachment_text(att)
        if extracted_text:
            bot.doc_cache.store_document("text_file", att.filename, extracted_text, msg.id, msg.channel.id)


def render_history_body(msg):
    text = msg.content or ""
    doc_blocks = bot.doc_cache.render_blocks(msg.id)
    if doc_blocks:
        text += "\n\n" + "\n\n".join(doc_blocks)
    return text


async def build_history(message, config, guard_sexual, current_display_name):
    """블록 창 히스토리를 Responses input으로 만든다. 반환: (history, speaker_labels)

    차단·거부된 메시지와 아직 검사 안 된 메시지 중 걸린 것은 뺀다. 봇 답장은 assistant, 나머지는 `표시명:` + 본문 +
    문서 전문 + 이미지 (Gemini·LLM 봇과 같은 모양 → 위치와 상관없이 늘 같은 모양 → 캐시 유지).
    """
    reset_dt = datetime.fromisoformat(config["reset_time"]) if config.get("reset_time") else None
    history_limit = get_history_limit(config)
    history_block = get_history_block(config)
    media_cap = get_image_history_limit(config)

    # 1) 히스토리 후보를 모은다 (최신 → 과거). 창 범위를 먼저 정하고 나서 내용을 만든다.
    candidates = []
    scan_limit = history_limit + history_block + 20
    scanned = 0
    oldest_scanned_id = None
    hit_reset = False
    async for msg in message.channel.history(limit=scan_limit):
        scanned += 1
        oldest_scanned_id = msg.id
        if msg.id == message.id:
            continue
        if msg.id > message.id and msg.author != bot.user:
            continue
        if reset_dt and msg.created_at < reset_dt:
            hit_reset = True
            break
        # 슬래시 명령 결과와 오류 안내는 대화가 아니다
        if msg.author == bot.user and (getattr(msg, "interaction_metadata", None) is not None or is_bot_notice(msg.content)):
            continue
        if bot.guard.is_excluded(msg.id, guard_sexual):
            continue
        if not (msg.content or "").strip() and not msg.attachments and not bot.doc_cache.get_docs_for_message(msg.id):
            continue
        candidates.append(msg)
    candidates.reverse()

    # 2) 블록 창을 고른다. 블록 동안 시작점이 고정돼 앞부분이 요청마다 같게 유지된다.
    image_plans = [plan_history_images(msg, enabled=media_cap > 0) for msg in candidates]
    anchor_id = bot.history_anchors.get(message.channel.id)
    covered = hit_reset or scanned < scan_limit or (
        anchor_id is not None and oldest_scanned_id is not None and oldest_scanned_id <= anchor_id
    )
    window_start, window_reason = select_history_window(
        [msg.id for msg in candidates],
        anchor_id=anchor_id,
        covered=covered,
        base=history_limit,
        block=history_block,
        media_counts=[len(plan) for plan in image_plans],
        media_bytes=[sum(estimated_image_bytes(att) for att in plan) for plan in image_plans],
        media_cap=media_cap,
        media_bytes_cap=HISTORY_MEDIA_BYTES_CAP,
    )
    window = candidates[window_start:]
    window_images = fit_history_images_to_budget(image_plans[window_start:], HISTORY_MEDIA_BYTES_CAP)
    bot.history_anchors[message.channel.id] = window[0].id if window else None
    # 히스토리가 더는 볼 수 없는(이번에 훑은 범위보다 오래된) 메시지의 검사·거부 기록은 지운다
    pruned = bot.guard.prune(message.channel.id, oldest_scanned_id or message.id)

    # 3) 사용자 메시지마다 본문·이미지를 준비하고, 검사 기록이 없거나 내용이 바뀐 메시지를 한 번에 검사한다
    rows = []
    for msg, planned in zip(window, window_images):
        if msg.author == bot.user:
            rows.append({"msg": msg, "self": True, "planned": planned})
            continue
        await ensure_history_documents(msg)
        all_images = [att for att in msg.attachments if is_image_attachment(att)]
        content_hash = guard_hash(msg, all_images)
        rows.append({
            "msg": msg,
            "self": False,
            "body": render_history_body(msg),
            "planned": planned,
            "all_images": all_images,
            "hash": content_hash,
            "entry": bot.guard.get(msg.id, content_hash),
        })
    needed = {}
    for row in rows:
        unscreened_images = [] if row["self"] or row["entry"] is not None else row["all_images"]
        for att in row["planned"] + unscreened_images:
            needed[att.id] = att
    prepared = dict(zip(needed, await asyncio.gather(*(bot.images.get(att) for att in needed.values()))))

    unscreened = [row for row in rows if not row["self"] and row["entry"] is None]
    if unscreened:
        items = [
            (row["msg"].id, [row["body"]], [prepared[att.id].data_url() for att in row["all_images"] if prepared.get(att.id)])
            for row in unscreened
        ]
        verdicts, failed = await moderate(items, partial=True)
        for row in unscreened:
            if row["msg"].id in verdicts:
                flags, minors = verdicts[row["msg"].id]
                bot.guard.put(row["msg"].id, message.channel.id, row["hash"], flags, minors)
                row["entry"] = bot.guard.get(row["msg"].id)
        if failed:
            # 검사 못 한 메시지는 이번 요청에서 뺀다 (기록하지 않으므로 다음 메시지에서 다시 검사)
            print(f"[GUARD] 히스토리 {len(failed)}개 검사 실패 → 이번 요청에서 뺌 ({next(iter(failed.values()))})")

    kept = []
    blocked_now = 0
    for row in rows:
        if not row["self"]:
            if row["entry"] is None:
                continue
            if guard_reasons(row["entry"], guard_sexual):
                blocked_now += 1
                continue
        kept.append(row)

    # 4) 대화 턴으로 만든다
    speaker_labels = build_speaker_labels(
        [(row["msg"].author.id, get_server_display_name(message.guild, row["msg"].author)) for row in kept if not row["self"]]
        + [(message.author.id, current_display_name)]
    )
    history = []
    image_count = 0
    for row in kept:
        msg = row["msg"]
        if row["self"]:
            text = strip_bot_name_prefix(msg.content or "")
            if text.strip():
                history.append({"role": "assistant", "content": text})
            own_images = [prepared[att.id].block() for att in row["planned"] if prepared.get(att.id)]
            if own_images:
                # 입력 이미지는 사용자 턴에만 넣을 수 있어, 봇이 만든 이미지임을 밝혀 사용자 턴으로 보낸다
                history.append({"role": "user", "content": [{"type": "input_text", "text": OWN_IMAGE_LABEL}, *own_images]})
                image_count += len(own_images)
            continue
        label = speaker_labels.get(msg.author.id) or get_server_display_name(message.guild, msg.author)
        blocks = [{"type": "input_text", "text": f"{label} ({format_sent_time(msg.created_at)}):\n{row['body']}"}]
        for att in row["planned"]:
            item = prepared.get(att.id)
            if item:
                blocks.append(item.block())
                image_count += 1
        history.append({"role": "user", "content": blocks})
    # 대화는 사용자 턴으로 시작해야 한다. 창 맨 앞의 봇 답장은 뺀다 (블록 동안 늘 같은 것이 빠지므로 캐시 영향 없음).
    while history and history[0]["role"] == "assistant":
        history.pop(0)
    print(
        f"[HISTORY] window={len(window)} (base {history_limit} + block {history_block}) reason={window_reason} "
        f"turns={len(history)} images={image_count}/{media_cap} screened={len(unscreened)} "
        f"blocked={blocked_now} pruned={pruned}"
    )
    return history, speaker_labels


async def handle_refusal(message, config, result, display_name):
    """GPT가 거부: 🚫 반응만 달고(답장 없음) 이 메시지를 히스토리에서 뺀다. 다시 시도하지 않는다."""
    bot.guard.mark_refused(message.id, message.channel.id)
    bot.counters["refused"] += 1
    streak = bot.refusal_streaks.get(message.channel.id, 0) + 1
    bot.refusal_streaks[message.channel.id] = streak
    print(
        f"[REFUSAL] msg={message.id} {display_name}: streak={streak} "
        f"explanation={result.refusal_explanation!r} text={(message.content or '')[:80]!r}"
    )
    await add_reaction(message, REACT_BLOCKED)
    if streak >= REFUSAL_RESET_STREAK:
        config["reset_time"] = datetime.now(timezone.utc).isoformat()
        bot.save_config()
        bot.history_anchors.pop(message.channel.id, None)
        bot.refusal_streaks[message.channel.id] = 0
        print(f"[REFUSAL] 연속 {streak}회 거부 → 히스토리를 새로 시작")
        await add_reaction(message, REACT_HISTORY_RESET)


async def respond(message, config):
    started = time.perf_counter()
    model = get_model(config)
    effort = get_effort(config)
    guard_sexual = bool(config.get("guard_sexual", True))
    display_name = get_server_display_name(message.guild, message.author)

    # 1) 현재 메시지: 링크 본문 → 첨부 (Gemini·LLM 봇과 같은 순서)
    user_text, unread_urls = await read_links(message, message.content or "")
    user_text, images, screen_images = await read_current_attachments(message, user_text)

    # 2) 사전 검사. 통과해야 GPT에 보낸다. 검사를 못 하면 보내지 않고 ⚠️만 단다.
    content_hash = guard_hash(message, [att for att in message.attachments if is_image_attachment(att)])
    guard_started = time.perf_counter()
    try:
        verdicts = await moderate([(message.id, [user_text], [item.data_url() for item in screen_images])])
    except GuardUnavailable as e:
        bot.counters["guard_errors"] += 1
        print(f"[GUARD] 검사 실패 → GPT에 보내지 않음 msg={message.id} ({e})")
        await add_reaction(message, REACT_GUARD_ERROR)
        return
    flags, minors = verdicts[message.id]
    bot.guard.put(message.id, message.channel.id, content_hash, flags, minors)
    reasons = guard_reasons(bot.guard.get(message.id), guard_sexual)
    if reasons:
        bot.counters["blocked"] += 1
        print(f"[GUARD] 차단 msg={message.id} {display_name}: {reasons} text={(message.content or '')[:80]!r}")
        await add_reaction(message, REACT_BLOCKED)
        return
    guard_ms = (time.perf_counter() - guard_started) * 1000

    # 3) 히스토리 + 현재 메시지 (시각 줄 → `표시명:` → 본문(웹페이지·문서 포함) → 이미지)
    history, speaker_labels = await build_history(message, config, guard_sexual, display_name)
    mark_cache_breakpoint(history)
    label = speaker_labels.get(message.author.id) or display_name
    current = [{"type": "input_text", "text": f"{format_current_time_marker()}\n{label}:\n{user_text}"}]
    current.extend(item.block() for item in images)
    messages = history + [{"role": "user", "content": current}]

    # 4) GPT
    tally = UsageTally()
    sources = []
    result = await generate_reply(
        model,
        messages,
        effort=effort,
        user_hash=hash_user_id(message.author.id),
        cache_key=f"discord:{message.channel.id}",
        backend=pick_search_backend(config, unread_urls),
        image_tool=build_image_tool(config),
        tally=tally,
        sources=sources,
    )
    bot.stats.merge(tally)
    if result.stop_reason == "refusal":
        await handle_refusal(message, config, result, display_name)
        return
    bot.refusal_streaks[message.channel.id] = 0
    bot.counters["replies"] += 1

    response = clean_response_text(result.text, speaker_labels)
    if response and result.stop_reason == "max_output_tokens":
        response += "\n\n(⚠️ 길이 제한으로 답이 잘렸습니다)"
    sources_block = format_search_sources(sources, response)
    if response and sources_block:
        response = f"{response}\n\n{sources_block}"

    log_time = datetime.now().strftime("%H:%M:%S")
    print(f"[{log_time}] [{message.guild.name}] {display_name}: {message.content}")
    print(
        f"봇({model['id']} effort={effort or 'default'} search={result.backend}): {response} "
        f"({tally.describe()}, guard {guard_ms:.0f}ms, total {time.perf_counter() - started:.1f}s)"
    )
    print("-------")

    if response:
        await send_long_message(message.channel, response)
    await send_images(message.channel, result.images)
    if result.image_blocked and not result.images:  # 막힌 뒤 모델이 고쳐 그린 경우는 결과가 나왔으니 알리지 않는다
        await message.channel.send("⚠️ 이미지가 OpenAI 안전 필터에 막혔습니다. 요청을 바꿔 다시 시도해 주세요.")
    elif not response and not result.images:
        await message.channel.send(f"⚠️ 빈 응답을 받았습니다 (stop_reason: {result.stop_reason})")


# ================= 9. 이벤트 =================
@bot.event
async def on_ready():
    print(f"🤖 로그인: {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    config = bot.get_config(message.guild.id)
    if message.channel.id != config.get("channel_id"):
        return
    if not (message.content or "").strip() and not message.attachments:
        return  # 스티커만 있는 메시지 등

    lock = bot.get_channel_lock(message.channel.id)
    async with lock:
        async with message.channel.typing():
            try:
                await respond(message, config)
            except Exception as e:
                print(f"❌ 오류: {e}")
                traceback.print_exc()
                await message.channel.send(f"오류가 발생했습니다: {str(e)[:1500]}")
            finally:
                bot.guard.save()

# ================= 10. 명령어 =================
@bot.tree.command(name="set", description="현재 채널을 GPT 봇 채널로 설정")
async def set_channel(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    config["channel_id"] = interaction.channel_id
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 GPT 봇 채널을 {interaction.channel.mention} 으로 설정했습니다."
    )


@bot.tree.command(name="reset", description="히스토리 초기화")
async def reset_history(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    bot.save_config()
    channel_id = config.get("channel_id")
    if channel_id:
        bot.history_anchors.pop(channel_id, None)
        bot.refusal_streaks.pop(channel_id, None)
        bot.guard.clear_channel(channel_id)
        bot.guard.save()
    await interaction.response.send_message("[OK] 히스토리 초기화")


@bot.tree.command(name="history", description="GPT 히스토리 개수·블록 크기·이미지 상한 설정")
@app_commands.describe(
    count=f"항상 남기는 최소 히스토리 메시지 개수 ({MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)",
    block=f"블록 크기: 히스토리가 count+block개가 되면 오래된 block개를 한 번에 버림 ({MIN_HISTORY_BLOCK}~{MAX_HISTORY_BLOCK})",
    image_count=f"히스토리 속 이미지 상한, 넘으면 블록을 일찍 교체 ({MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=끄기)",
)
async def set_history(
    interaction: discord.Interaction,
    count: int | None = None,
    block: int | None = None,
    image_count: int | None = None,
):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    if count is None and block is None and image_count is None:
        base = get_history_limit(config)
        block_size = get_history_block(config)
        await interaction.response.send_message(
            f"현재 GPT 히스토리: 최소 **{base}개**, 블록 **{block_size}개** → {base}~{base + block_size - 1}개 사이를 오감 "
            f"(사용자+봇 합산)\n"
            f"현재 GPT 히스토리 이미지 상한: **{get_image_history_limit(config)}개** "
            f"(범위 {MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=끄기)"
        )
        return
    changed = []
    if count is not None:
        limit = clamp_history_limit(count)
        config["history_limit"] = limit
        suffix = "" if limit == count else f" (요청값 {count}에서 허용 범위로 보정)"
        changed.append(f"최소 히스토리 **{limit}개**{suffix}")
    if block is not None:
        block_size = clamp_history_block(block)
        config["history_block"] = block_size
        suffix = "" if block_size == block else f" (요청값 {block}에서 허용 범위로 보정)"
        changed.append(f"블록 **{block_size}개**{suffix}")
    if image_count is not None:
        image_limit = clamp_image_history_limit(image_count)
        config["image_history_limit"] = image_limit
        suffix = "" if image_limit == image_count else f" (요청값 {image_count}에서 허용 범위로 보정)"
        changed.append(f"히스토리 이미지 상한 **{image_limit}개**{suffix}")
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 GPT " + ", ".join(changed) + "로 설정했습니다."
    )


def format_status(config):
    model = get_model(config)
    price_in, price_out, price_read, _ = model["price"]
    base = get_history_limit(config)
    block = get_history_block(config)
    stats = bot.stats
    counters = bot.counters
    cache_rate = f"{stats.cache_read * 100 // stats.prompt_tokens}%" if stats.prompt_tokens else "-"
    return (
        f"모델: **{model['name']}** (`{model['id']}`) · 사고 강도 {describe_effort(config)}\n"
        f"요금(100만 토큰): 입력 ${price_in:g} / 출력 ${price_out:g} / 캐시 읽기 ${price_read:g}\n"
        f"이미지: {describe_image_settings(config)}\n"
        f"검색: {describe_search(config)}\n"
        f"사전 검사: OpenAI Moderation · 성인 노골적 텍스트 차단 **{'켜짐' if config.get('guard_sexual', True) else '꺼짐'}**\n"
        f"히스토리: {base}~{base + block - 1}개 (블록 {block}) / 이미지 상한 {get_image_history_limit(config)}개\n"
        f"봇 시작 후: 답장 {counters['replies']} · 차단 {counters['blocked']} · 거부 {counters['refused']} · "
        f"검사 실패 {counters['guard_errors']} · 입력 {stats.prompt_tokens:,}토큰(캐시 {cache_rate}) · "
        f"출력 {stats.output_tokens:,}토큰 · 이미지 {stats.images}장 · 약 ${stats.cost:.4f}"
    )


@bot.tree.command(name="model", description="GPT 모델·사고 강도 전환 (비우면 현재 상태)")
@app_commands.describe(name="모델", effort="사고 강도 (비우면 모델 기본값, none은 Luna·Terra만)")
@app_commands.choices(
    name=[app_commands.Choice(name=info["name"], value=key) for key, info in MODELS.items()],
    effort=[app_commands.Choice(name="기본값", value="default")]
    + [app_commands.Choice(name=level, value=level) for level in EFFORT_LEVELS],
)
async def switch_model(interaction: discord.Interaction, name: str | None = None, effort: str | None = None):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    if name is None and effort is None:
        await interaction.response.send_message(format_status(config))
        return
    target = MODELS.get(name) if name is not None else get_model(config)
    if target is None:
        await interaction.response.send_message(f"[ERROR] 없는 모델입니다: {name}", ephemeral=True)
        return
    if effort not in (None, "default") and effort not in target["efforts"]:
        await interaction.response.send_message(
            f"[ERROR] {target['name']}은(는) 사고 강도 `{effort}`를 지원하지 않습니다.", ephemeral=True
        )
        return
    if name is not None:
        config["model"] = name
        config["effort"] = None  # 모델을 바꾸면 사고 강도는 그 모델 기본값으로
    if effort is not None:
        config["effort"] = None if effort == "default" else effort
    bot.save_config()
    model = get_model(config)
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 바꿨습니다: **{model['name']}** (`{model['id']}`) · 사고 강도 {describe_effort(config)}"
    )


@bot.tree.command(name="image", description="이미지 생성 모델·품질·크기 설정 (비우면 현재 상태)")
@app_commands.describe(model="이미지 모델", quality="품질 (높을수록 비쌈)", size="크기 (가로x세로)")
@app_commands.choices(
    model=[app_commands.Choice(name=info["name"], value=key) for key, info in IMAGE_MODELS.items()],
    quality=[app_commands.Choice(name=level, value=level) for level in IMAGE_QUALITIES],
    size=[app_commands.Choice(name=value, value=value) for value in IMAGE_SIZES],
)
async def image_settings(
    interaction: discord.Interaction,
    model: str | None = None,
    quality: str | None = None,
    size: str | None = None,
):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    if model is None and quality is None and size is None:
        await interaction.response.send_message(f"현재 이미지 설정: {describe_image_settings(config)}")
        return
    if model is not None:
        config["image_model"] = model
    if quality is not None:
        config["image_quality"] = quality
    if size is not None:
        config["image_size"] = size
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(f"[OK] {actor_name}님이 바꿨습니다: {describe_image_settings(config)}")


@bot.tree.command(name="search", description="웹 검색 방식 선택 (비우면 현재 상태)")
@app_commands.describe(mode="검색 방식")
@app_commands.choices(mode=[app_commands.Choice(name=label, value=key) for key, label in SEARCH_MODES.items()])
async def search_mode(interaction: discord.Interaction, mode: str | None = None):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    if mode is None:
        await interaction.response.send_message(f"현재 검색: {describe_search(config)}")
        return
    config["search_mode"] = mode
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(f"[OK] {actor_name}님이 검색을 **{SEARCH_MODES[mode]}**(으)로 바꿨습니다.")


@bot.tree.command(name="guard", description="사전 검사 설정 (성인 대상 노골적 성적 텍스트 차단 켜기/끄기)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(sexual="성인 대상 노골적 성적 텍스트 차단. 미성년자·폭력 범죄 방법·자해 방법·노골적 이미지는 항상 차단")
@app_commands.choices(sexual=[
    app_commands.Choice(name="켜기 (기본)", value="on"),
    app_commands.Choice(name="끄기", value="off"),
])
async def guard_command(interaction: discord.Interaction, sexual: str | None = None):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    if sexual is None:
        await interaction.response.send_message(
            f"사전 검사(OpenAI Moderation): 성인 대상 노골적 성적 텍스트 차단 "
            f"**{'켜짐' if config.get('guard_sexual', True) else '꺼짐'}**\n"
            "항상 차단: 미성년자 성적 내용 · 무기/폭력 범죄 방법 · 자해 방법 · 노골적 이미지"
        )
        return
    config["guard_sexual"] = sexual == "on"
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 성인 대상 노골적 성적 텍스트 차단을 **{'켰습니다' if config['guard_sexual'] else '껐습니다'}**."
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
