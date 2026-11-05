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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

from prompt_inject import (
    REFUSAL_NUDGE, PromptInjector, refusal_hits, resolve_inject_path, sync_from_template,
)

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
DISCORD_TOKEN = os.environ["DEEPSEEK_DISCORD_TOKEN"]
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise SystemExit("DEEPSEEK_API_KEY 환경 변수가 필요합니다.")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# GPT·Claude 봇과 달리 OpenAI Moderation 사전 검사는 하지 않는다.
# 웹 검색은 DeepSeek Search API(기본) 또는 Tavily. 둘 다 함수 도구라 모델과 상관없이 쓴다.
# Responses API의 내장 web_search는 Pro에서만 동작해서 쓰지 않는다.
DEEPSEEK_SEARCH_URL = f"{DEEPSEEK_BASE_URL}/v1/tools/search"
DEEPSEEK_FETCH_URL = f"{DEEPSEEK_BASE_URL}/v1/tools/fetch"
DEEPSEEK_TOOL_PRICE = 0.00075  # Search·Fetch 성공 1회 요금
DEEPSEEK_QUERY_MAX_CHARS = 128  # Search API 검색어 상한
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
SEARCH_MAX_RESULTS = 5  # 모델이 결과 수를 정하지 않았을 때 쓰는 값
SEARCH_MAX_RESULTS_CAP = 10  # 모델이 고를 수 있는 결과 수 상한 (토큰 안전장치)
SEARCH_RESULT_CHARS = 2000  # 결과 하나당 본문 최대 길이 (토큰 안전장치)
SEARCH_OUTAGE_COOLDOWN = 600  # 검색 키·잔액·서버 문제가 나면 이 시간(초) 동안 다른 검색을 쓴다
WEB_SEARCH_MAX_CALLS = 5  # 한 메시지에서 검색 호출 총 상한
FETCH_MAX_CALLS = 5  # 한 메시지에서 페이지 읽기 호출 총 상한
TOOL_MAX_ROUNDS = WEB_SEARCH_MAX_CALLS + FETCH_MAX_CALLS  # 한 번에 하나씩 불러도 한도를 다 쓸 수 있게 왕복 수를 맞춘다
# DeepSeek는 붐빌 때 연결만 열어 두고 keep-alive로 최대 10분 기다리게 한다. 그동안 채널 잠금이 잡혀
# 뒤 메시지가 전부 막히므로, 이벤트가 이 시간(초) 동안 하나도 안 오면 포기한다.
STREAM_IDLE_TIMEOUT = 150
SEARCH_MODES = {"deepseek": "DeepSeek 검색", "tavily": "Tavily"}
DEFAULT_SEARCH_MODE = "deepseek"

# 가격은 100만 토큰당 달러: (입력, 출력, 캐시 읽기). 할인 시간대 기준이고 피크 시간대는 두 배다.
# 로그에 찍는 예상 비용 계산용이다. 캐시는 자동이라 쓰기 요금이 없다.
# 사고 강도를 비우면 API 기본값(high). none은 사고를 끈다. medium·xhigh는 API가 high로 바꾸므로 뺐다.
EFFORT_LEVELS = ("none", "low", "high", "max")
API_DEFAULT_EFFORT = "high"
MODELS = {
    "flash": {
        "id": "deepseek-flash",
        "name": "DeepSeek V4.1 Flash",
        "vision": True,
        "price": (0.15, 0.60, 0.003),
    },
    "pro": {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "vision": False,  # 이미지를 보내면 오류 없이 못 본다고 답한다 (실측)
        "price": (0.66, 1.98, 0.022),
    },
}
DEFAULT_MODEL_KEY = "flash"
VISION_FALLBACK_MODEL_KEY = "flash"  # 이미지를 못 보는 모델일 때 이미지가 있는 메시지는 이 모델로 답한다
# 피크 시간대(UTC, 월~금): 01:00–04:00, 06:00–10:00. 중국 공휴일 예외는 따지지 않는다.
PEAK_HOURS_UTC = ((1, 4), (6, 10))
MAX_OUTPUT_TOKENS = 25000  # 사고 토큰도 여기에 포함된다
MAX_OUTPUT_TOKENS_DEEP = 64000  # 사고 강도 max

# 🔢 [수치 조절 구역]
# 히스토리는 블록 단위로 자른다: 창이 HISTORY_LIMIT개에서 시작해 HISTORY_LIMIT + 블록 - 1개까지 뒤로만 늘고,
# 그다음 메시지에서 오래된 블록 하나를 한 번에 버린다. 블록 동안 앞부분이 그대로라 자동 캐시가 맞는다.
HISTORY_LIMIT = 20
MIN_HISTORY_LIMIT = 1
MAX_HISTORY_LIMIT = 100
HISTORY_BLOCK = 20
MIN_HISTORY_BLOCK = 1
MAX_HISTORY_BLOCK = 100
# 창 안 이미지는 전부 다시 보낸다. 이 개수를 넘으면 블록을 일찍 교체한다 (0이면 히스토리 이미지 끔).
IMAGE_HISTORY_LIMIT = int(os.environ.get("DEEPSEEK_IMAGE_HISTORY_LIMIT", "10"))
MIN_IMAGE_HISTORY_LIMIT = 0
MAX_IMAGE_HISTORY_LIMIT = 20
# 이미지는 변 2000px 안으로 맞춘다 (넘으면 줄여서 JPEG로 다시 저장).
IMAGE_MAX_EDGE = 2000
IMAGE_MAX_BYTES = 2 * 1024 * 1024  # 이보다 큰 이미지도 JPEG로 다시 저장
IMAGE_SOURCE_MAX_BYTES = 25 * 1024 * 1024  # 원본이 이보다 크면 받지 않는다
HISTORY_MEDIA_BYTES_CAP = 10 * 1024 * 1024
CURRENT_MEDIA_BYTES_CAP = 10 * 1024 * 1024
IMAGE_CACHE_BYTES = 64 * 1024 * 1024  # 히스토리 이미지를 메시지마다 다시 받지 않게 메모리에 두는 양
MAX_DOC_CHARS = 12000
MAX_DOC_SOURCE_BYTES = 20 * 1024 * 1024
MAX_LINKS_PER_MESSAGE = 5
MIN_SCRAPED_CHARS = 200  # 직접 읽은 본문이 이보다 짧으면(자바스크립트 페이지 등) DeepSeek Fetch로 다시 읽는다
MAX_SCRAPE_BYTES = 3 * 1024 * 1024  # 직접 읽을 때 받는 HTML 상한

# 읽을 수 있는 텍스트 파일 확장자
SUPPORTED_EXTENSIONS = ('.txt', '.py', '.c', '.cpp', '.java', '.js', '.html',
                        '.css', '.md', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
                        '.sh', '.rb', '.go', '.rs', '.csv', '.tsv', '.log')
DOCUMENT_EXTENSIONS = SUPPORTED_EXTENSIONS + ('.pdf', '.docx', '.xlsx')
IMAGE_ATTACHMENT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# 히스토리에서 빼는 봇 안내 메시지 (오류 등)
BOT_NOTICE_PREFIXES = ("오류가 발생했습니다", "⚠️")
NO_VISION_IMAGE_NOTE = "(An image was attached here, but the current model cannot see images.)"

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

# DeepSeek 봇 구조상 필요한 안내만 덧붙인다. 요청마다 같은 문자열이어야 자동 캐시가 맞는다.
# 검색 도구는 DeepSeek이든 Tavily든 이름이 web_search라 안내 문구가 같다.
DEEPSEEK_RULES = (
    "You have a `web_search` tool. Use it for anything time-sensitive or that may have changed after your training "
    "(news, prices, releases, schedules) instead of guessing. Don't search for casual chat.",
    "You have a `fetch_url` tool that reads a web page. Use it when a search summary is not enough or to open a "
    "link from the conversation.",
    "You cannot create images, watch videos, or listen to audio. If asked, say so.",
)
DEEPSEEK_BASE_PROMPT = SYSTEM_PROMPT.rstrip() + "\n" + "\n".join(f"- {rule}" for rule in DEEPSEEK_RULES) + "\n"


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
DATA_DIR = os.path.dirname(CONFIG_FILE)
DOC_CACHE_DIR = os.environ.get("DEEPSEEK_DOC_CACHE_DIR", os.path.join(DATA_DIR, "deepseek_doc_cache"))
KST = timezone(timedelta(hours=9))

# 거절 가드 재생성 횟수. 0 이면 끔. 재생성은 거절 문구가 나왔을 때만 1회 더 호출한다.
try:
    REFUSAL_RETRIES = max(0, int(os.environ.get("DEEPSEEK_REFUSAL_RETRIES", "1")))
except ValueError:
    REFUSAL_RETRIES = 1

# dsh-purge 의 prompt-inject.md 자리를 봇 쪽으로 옮긴 것. 본문은 파일에 있고 여기서는 읽기만 한다.
# 파일이 바뀌면 다음 요청부터 반영되고, 그대로면 같은 문자열이 유지되어 DeepSeek 접두사 캐시가 깨지지 않는다.
PROMPT_INJECT = PromptInjector(
    resolve_inject_path(BASE_DIR, DATA_DIR),
    DEEPSEEK_BASE_PROMPT,
    label="NRDeepSeek",
)


def auto_sync_prompt_inject():
    """DEEPSEEK_PROMPT_AUTOSYNC=1 이면 이미지가 들고 온 본문을 실제 경로에 반영한다.

    재배포만으로 프롬프트가 따라오게 하는 opt-in. 기본은 꺼짐 —
    켜지 않으면 서버에서 직접 고친 본문이 그대로 유지된다. 덮어쓰기 전에 백업한다.
    """
    if os.environ.get("DEEPSEEK_PROMPT_AUTOSYNC", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    template = os.path.join(BASE_DIR, "prompt-inject.md")
    live = PROMPT_INJECT.path
    try:
        changed, backup, snap = sync_from_template(template, live, os.path.join(DATA_DIR, ".nr-purge"))
    except OSError as exc:
        print(f"[PURGE] 자동 동기화 실패: {type(exc).__name__}: {exc}")
        return
    if changed:
        print(f"[PURGE] 프롬프트 자동 동기화: {template} → {live} ({snap.size:,}자, 백업 {backup})")
    else:
        print(f"[PURGE] 프롬프트 동기화 불필요 (이미 최신: {snap.size:,}자)")


def system_prompt():
    """요청마다 부른다 (베이스 프롬프트 + ~/.dsh/prompt-inject.md 에 해당하는 파일)."""
    return PROMPT_INJECT.system_prompt()


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
    def over_cap(begin):
        # media_cap 0은 개수 상한 없이 창에 담긴 만큼 쓴다 (용량 상한은 그대로 본다)
        over_count = media_cap > 0 and sum(media_counts[begin:]) > media_cap
        return over_count or sum(media_bytes[begin:]) > media_bytes_cap

    early_start = max(start, total - base)
    if early_start > start and over_cap(start) and not over_cap(early_start):
        start, reason = early_start, "media"
    return start, reason


def estimated_image_bytes(att):
    """다운로드 전 크기 추정: 큰 이미지는 IMAGE_MAX_BYTES 이하로 다시 저장된다."""
    return min(att.size or 0, IMAGE_MAX_BYTES)


def plan_history_images(msg, *, enabled):
    """히스토리 메시지에서 다시 보낼 이미지 첨부 (다운로드 전, 첨부 메타데이터만 본다)."""
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


# ================= 4. 이미지 =================
IMAGE_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
EXIF_ORIENTATION = 0x0112


class PreparedImage:
    """DeepSeek에 그대로 넣는 이미지 (base64)."""

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
    """원본을 DeepSeek가 받는 이미지로 맞춘다. 반환 (bytes, media_type) 또는 None(못 읽음).

    JPEG/PNG/GIF/WebP이고 변 IMAGE_MAX_EDGE·IMAGE_MAX_BYTES 이하면 원본 그대로 둔다. 아니면(움직이는 이미지,
    회전 정보가 있는 사진, 큰 이미지, 다른 형식) 첫 프레임을 IMAGE_MAX_EDGE 안으로 줄여 JPEG로 다시 저장한다.
    같은 입력이면 늘 같은 바이트가 나와 히스토리로 다시 보낼 때도 캐시가 맞는다.
    """
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            media_type = IMAGE_MEDIA_TYPES.get((img.format or "").upper())
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

def hash_user_id(user_id):
    """Responses API user 값. 디스코드 ID 대신 해시를 보낸다."""
    return hashlib.sha256(f"nr-discord-deepseek:{user_id}".encode("utf-8")).hexdigest()

# ================= 5. 웹 검색 / 링크 읽기 =================
# 검색 방법(검색어·언어·출처·기간·깊이·횟수)은 모델이 정한다. 설명에는 각 옵션이 하는 일과 비용만 적는다.
DEEPSEEK_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web (DeepSeek search engine, strong on Korean and Chinese pages). Each result has a title, URL, "
        "published date when known, and a summary of the page. The query supports operators: site:example.com, "
        "lang:ko,en, after:YYYY-MM-DD, before:YYYY-MM-DD. "
        f"Up to {WEB_SEARCH_MAX_CALLS} searches per message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": f"Search query, at most {DEEPSEEK_QUERY_MAX_CHARS} characters including operators.",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": SEARCH_MAX_RESULTS_CAP,
                "description": f"Number of results (default {SEARCH_MAX_RESULTS}).",
            },
        },
        "required": ["query"],
    },
}

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
                "maximum": SEARCH_MAX_RESULTS_CAP,
                "description": f"Number of results (default {SEARCH_MAX_RESULTS}).",
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

FETCH_TOOL = {
    "type": "function",
    "name": "fetch_url",
    "description": (
        "Read a web page and return its main text as Markdown. "
        f"Up to {FETCH_MAX_CALLS} pages per message."
    ),
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL of the page."}},
        "required": ["url"],
    },
}

SEARCH_TOOLS = {"deepseek": DEEPSEEK_SEARCH_TOOL, "tavily": TAVILY_SEARCH_TOOL}
SEARCH_STATE = {name: {"down_until": 0.0, "reason": None} for name in SEARCH_MODES}


class SearchOutage(Exception):
    """키·잔액·서버 문제처럼 모델이 검색어를 고쳐도 풀리지 않는 검색 실패."""

    def __init__(self, backend, reason):
        super().__init__(f"{backend}: {reason}")
        self.backend = backend
        self.reason = reason


def is_outage_status(status):
    # 400은 모델이 고른 옵션 문제라 모델에게 돌려준다. 401·403 키, 402·432·433 잔액·크레딧, 429 속도, 5xx·네트워크는 장애로 본다.
    return status is None or status in (401, 402, 403, 429, 432, 433) or status >= 500


def search_available(backend):
    if backend == "tavily" and not TAVILY_API_KEY:
        return False
    return time.time() >= SEARCH_STATE[backend]["down_until"]


def mark_search_down(backend, reason):
    SEARCH_STATE[backend]["down_until"] = time.time() + SEARCH_OUTAGE_COOLDOWN
    SEARCH_STATE[backend]["reason"] = str(reason)[:200]
    print(f"[SEARCH] {SEARCH_MODES[backend]} 사용 불가 → {SEARCH_OUTAGE_COOLDOWN // 60}분 동안 다른 검색 사용 ({reason})")


async def _post_json(url, headers, body, timeout_seconds):
    """JSON POST. 반환: (data, 오류 문자열, HTTP 상태 — 네트워크 오류면 None)"""
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


def _deepseek_headers():
    return {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}


def _tavily_headers():
    return {"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"}


async def deepseek_search(query, options=None):
    """DeepSeek Search. 반환: {"ok", "results", "error", "status", "options"(실제로 보낸 검색 옵션)}"""
    options = options or {}
    try:
        count = int(options.get("count") or SEARCH_MAX_RESULTS)
    except (TypeError, ValueError):
        count = SEARCH_MAX_RESULTS
    picked = {"count": min(max(count, 1), SEARCH_MAX_RESULTS_CAP)}
    if len(query) > DEEPSEEK_QUERY_MAX_CHARS:
        error = f"검색어가 {DEEPSEEK_QUERY_MAX_CHARS}자를 넘습니다 ({len(query)}자). 줄여서 다시 검색하세요."
        return {"ok": False, "results": [], "error": error, "status": 400, "options": picked}
    data, error, status = await _post_json(
        DEEPSEEK_SEARCH_URL, _deepseek_headers(), {"query": query, **picked}, timeout_seconds=30
    )
    if error:
        return {"ok": False, "results": [], "error": error, "status": status, "options": picked}
    results = [
        {
            "title": item.get("title") or "",
            "url": item["url"],
            # summary는 모델용 긴 요약, snippet은 화면용 짧은 글
            "content": (item.get("summary") or item.get("snippet") or "")[:SEARCH_RESULT_CHARS],
            "published_date": item.get("date"),
        }
        for item in data.get("results") or []
        if item.get("url")
    ]
    return {"ok": True, "results": results, "error": None, "status": status, "options": picked}


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
        max_results = int(options.get("max_results") or SEARCH_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = SEARCH_MAX_RESULTS
    picked["max_results"] = min(max(max_results, 1), SEARCH_MAX_RESULTS_CAP)
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
    data, error, status = await _post_json(TAVILY_SEARCH_URL, _tavily_headers(), body, timeout_seconds=40)
    if error:
        return {"ok": False, "results": [], "credits": None, "error": error, "status": status, "options": picked}
    results = [
        {
            "title": item.get("title") or "",
            "url": item["url"],
            "content": (item.get("content") or "")[:SEARCH_RESULT_CHARS],
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
            "검색어를 고쳐 다시 검색해도 된다. 검색 없이 답한다면 최신 정보가 필요한 부분은 확인이 필요하다고 밝혀라."
        )
    if not outcome.get("results"):
        return f"[web_search 결과 없음] query={query!r}"
    searched_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"[web_search 결과] query={query!r} (검색 시각: {searched_at})"]
    for i, item in enumerate(outcome["results"], start=1):
        date = f" ({item['published_date']})" if item.get("published_date") else ""
        lines.append(f"{i}. {item['title']}{date}\n   URL: {item['url']}\n   {item['content']}")
    return "\n".join(lines)


def make_tool_handler(backend, tally, sources):
    """메시지 하나 동안 쓰는 web_search·fetch_url 실행기. handler(name, args) → 도구 결과 텍스트

    호출 횟수를 도구마다 제한한다. 검색 결과(제목·URL)는 sources에 모아 답장 끝 출처 목록에 쓴다.
    검색 장애(키·잔액·서버)는 SearchOutage로 올려 다른 검색으로 다시 시도하게 한다.
    """
    state = {"web_search": 0, "fetch_url": 0}

    async def web_search(args):
        query = str(args.get("query") or "").strip()
        if not query:
            return "[web_search 실패] 검색어가 비어 있습니다."
        if state["web_search"] >= WEB_SEARCH_MAX_CALLS:
            return f"[web_search 중단] 이번 메시지의 검색 한도({WEB_SEARCH_MAX_CALLS}회)에 도달했습니다. 지금까지 찾은 결과로 답하세요."
        state["web_search"] += 1
        if backend == "tavily":
            outcome = await tavily_search(query, args)
            detail = f"credits={outcome.get('credits')}"
        else:
            outcome = await deepseek_search(query, args)
            detail = ""
            if outcome.get("ok"):
                tally.add_tool_call("search")
        count = len(outcome.get("results") or [])
        if not outcome.get("ok"):
            detail = f"error={outcome.get('error')}"
        print(
            f"[SEARCH] {backend} #{state['web_search']} query={query!r} opts={outcome.get('options')} "
            f"ok={outcome.get('ok')} results={count} {detail}"
        )
        if not outcome.get("ok") and is_outage_status(outcome.get("status")):
            raise SearchOutage(backend, outcome.get("error"))
        sources.extend(outcome.get("results") or [])
        return format_search_results(query, outcome)

    async def fetch_url(args):
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return f"[fetch_url 실패] http(s) 주소가 아닙니다: {url!r}"
        if state["fetch_url"] >= FETCH_MAX_CALLS:
            return f"[fetch_url 중단] 이번 메시지의 페이지 읽기 한도({FETCH_MAX_CALLS}회)에 도달했습니다."
        state["fetch_url"] += 1
        content, error = await deepseek_fetch(url, tally)
        if content is None:
            content = await scrap_webpage(url)
        print(f"[FETCH] #{state['fetch_url']} {url} ok={content is not None} {'' if content else f'error={error}'}")
        if not content:
            return f"[fetch_url 실패] {url}: {error or '본문을 읽지 못함'}. 내용을 추측하지 말 것."
        return f'[fetch_url] "{url}":\n\n{trim_document_text(content)}'

    async def handler(name, args):
        args = args or {}
        if name == "web_search":
            return await web_search(args)
        if name == "fetch_url":
            return await fetch_url(args)
        return f"알 수 없는 도구: {name}"

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


async def deepseek_fetch(url, tally=None):
    """DeepSeek Fetch로 URL 본문(Markdown)을 읽는다. 반환: (본문 또는 None, 오류)"""
    data, error, _status = await _post_json(DEEPSEEK_FETCH_URL, _deepseek_headers(), {"url": url[:8192]}, timeout_seconds=60)
    if error:
        return None, error
    content = (data.get("content") or "").strip()
    if not content:
        return None, "빈 본문"
    if tally is not None:
        tally.add_tool_call("fetch")
    return content, None


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


async def read_links(message, user_text, tally):
    """현재 메시지의 링크 본문을 읽어 user_text 끝에 붙인다.

    직접 읽기 → (PDF·자바스크립트 페이지·실패한 링크) DeepSeek Fetch. 그래도 못 읽으면 추측하지 말라고 적는다.
    """
    for url in find_urls(message.content):
        web_content = None
        if not urlsplit(url).path.lower().endswith(".pdf"):
            web_content = await scrap_webpage(url)
        how = "직접"
        if not web_content:
            web_content, error = await deepseek_fetch(url, tally)
            how = "DeepSeek Fetch"
        if web_content:
            doc_id = bot.doc_cache.store_document("webpage", url, web_content, message.id, message.channel.id)
            print(f"[WEB] {how} 읽음: {url} ({doc_id})")
            user_text += f'\n\nWebpage "{url}":\n\n{trim_document_text(web_content)}'
        else:
            print(f"[WEB] 링크 읽기 실패: {url} ({error})")
            user_text += f"\n\n(System: 이 링크의 본문을 읽지 못했습니다: {url} — 내용을 추측하지 말 것)"
    return user_text


# ================= 6. DeepSeek 호출 =================
@dataclass
class ReplyResult:
    text: str
    stop_reason: Optional[str]
    backend: Optional[str]


class DeepSeekBusy(Exception):
    """DeepSeek 서버가 붐벼서 답을 못 받음 (503·429·응답 없음)."""


def is_busy_error(error):
    if isinstance(error, (DeepSeekBusy, openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError)):
        return True
    return isinstance(error, openai.APIStatusError) and error.status_code in (502, 503, 504)


class ResponseFailed(Exception):
    """DeepSeek가 응답을 끝내지 못함 (response.failed)."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def is_peak_time(now=None):
    """DeepSeek 피크 시간대(요금 두 배)인지. 평일 UTC 01–04시, 06–10시."""
    now = now or datetime.now(timezone.utc)
    return now.weekday() < 5 and any(start <= now.hour < end for start, end in PEAK_HOURS_UTC)


class UsageTally:
    """토큰·비용 합계 (요청 하나, 또는 봇 시작 후 전체)."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0  # 캐시 읽기 포함
        self.cache_read = 0
        self.output_tokens = 0
        self.searches = 0  # DeepSeek Search 성공 횟수 (Tavily는 무료 크레딧이라 세지 않는다)
        self.fetches = 0  # DeepSeek Fetch 성공 횟수
        self.cost = 0.0

    @property
    def prompt_tokens(self):
        return self.input_tokens

    def add_response(self, model, response):
        def value(obj, name):
            return int(getattr(obj, name, 0) or 0) if obj is not None else 0

        usage = response.usage
        self.calls += 1
        if usage is None:
            return
        details = getattr(usage, "input_tokens_details", None)
        total_input = value(usage, "input_tokens")
        read = value(details, "cached_tokens")
        output = value(usage, "output_tokens")
        price_in, price_out, price_read = model["price"]
        multiplier = 2 if is_peak_time() else 1
        self.input_tokens += total_input
        self.cache_read += read
        self.output_tokens += output
        self.cost += multiplier * ((total_input - read) * price_in + read * price_read + output * price_out) / 1_000_000

    def add_tool_call(self, kind):
        if kind == "search":
            self.searches += 1
        else:
            self.fetches += 1
        self.cost += DEEPSEEK_TOOL_PRICE

    def merge(self, other):
        for name in ("calls", "input_tokens", "cache_read", "output_tokens", "searches", "fetches", "cost"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def describe(self):
        cache_rate = f" ({self.cache_read * 100 // self.prompt_tokens}%)" if self.prompt_tokens else ""
        extra = ""
        if self.searches:
            extra += f", DeepSeek 검색 {self.searches}회"
        if self.fetches:
            extra += f", 페이지 읽기 {self.fetches}회"
        return (
            f"In: {self.prompt_tokens}, Cached: {self.cache_read}{cache_rate}, "
            f"Out: {self.output_tokens}, calls={self.calls}{extra}, ~${self.cost:.5f}"
        )


async def call_deepseek(**request):
    # 사고 시간이 길 수 있어 스트리밍으로 받는다 (긴 요청의 연결 끊김 방지). 결과는 한 번에 쓴다.
    final = None
    loop = asyncio.get_running_loop()
    try:
        # 연결·재시도 포함 첫 이벤트까지, 그 뒤로는 이벤트 사이 간격마다 STREAM_IDLE_TIMEOUT
        async with asyncio.timeout(STREAM_IDLE_TIMEOUT) as deadline:
            # 끝 이벤트를 받아도 스트림을 끝까지 읽는다 (중간에 빠져나가면 연결 정리 중 오류가 난다)
            async with await bot.deepseek.responses.create(stream=True, **request) as stream:
                async for event in stream:
                    deadline.reschedule(loop.time() + STREAM_IDLE_TIMEOUT)
                    if event.type in ("response.completed", "response.incomplete", "response.failed"):
                        final = event.response
                    elif event.type == "error":
                        raise ResponseFailed(getattr(event, "code", None), getattr(event, "message", ""))
    except TimeoutError:
        raise DeepSeekBusy(f"{STREAM_IDLE_TIMEOUT}초 동안 응답이 없음") from None
    if final is None:
        raise RuntimeError("DeepSeek 스트림이 응답 없이 끝났습니다.")
    return final


async def run_deepseek(model, messages, *, effort, user_hash, backend, tally, sources, extra=""):
    """DeepSeek 호출 + 도구 루프. backend: deepseek | tavily (web_search 도구), None이면 검색 없이

    서버가 상태를 저장하지 않아 도구 왕복은 응답 항목(사고 포함)을 그대로 다시 보낸다.
    도구를 쓰는 대화에서 사고를 빼고 보내면 400이 난다 (DeepSeek 문서).
    """
    handler = make_tool_handler(backend, tally, sources)
    tools = ([SEARCH_TOOLS[backend]] if backend else []) + [FETCH_TOOL]
    convo = list(messages)
    tool_rounds = 0
    while True:
        request = {
            "model": model["id"],
            "instructions": system_prompt() + extra,
            "input": convo,
            "tools": tools,
            "max_output_tokens": MAX_OUTPUT_TOKENS_DEEP if effort == "max" else MAX_OUTPUT_TOKENS,
            "user": user_hash,
        }
        if effort:
            request["reasoning"] = {"effort": effort}
        if tool_rounds >= TOOL_MAX_ROUNDS:
            request["tool_choice"] = "none"
        response = await call_deepseek(**request)
        tally.add_response(model, response)
        if response.status == "failed":
            error = response.error
            raise ResponseFailed(getattr(error, "code", None), getattr(error, "message", "알 수 없는 오류"))

        answer_parts = []
        function_calls = []
        for item in (response.output or []):
            if item.type == "function_call":
                function_calls.append(item)
            elif item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        answer_parts.append(part.text)

        if function_calls and response.status == "completed":
            tool_rounds += 1
            convo.extend(item.to_dict() for item in (response.output or []))
            for call in function_calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except ValueError:
                    args = {}
                content = await handler(call.name, args if isinstance(args, dict) else {})
                convo.append({"type": "function_call_output", "call_id": call.call_id, "output": content})
            continue

        incomplete = getattr(response.incomplete_details, "reason", None) if response.incomplete_details else None
        return ReplyResult(text="".join(answer_parts).strip(), stop_reason=incomplete or response.status, backend=backend)


def search_fallback_order(config):
    """고른 검색 → 다른 검색 순서. 장애 중이거나 키가 없는 검색은 뺀다."""
    preferred = config.get("search_mode") if config.get("search_mode") in SEARCH_MODES else DEFAULT_SEARCH_MODE
    order = [preferred] + [name for name in SEARCH_MODES if name != preferred]
    return [name for name in order if search_available(name)]


async def generate_reply(model, messages, *, effort, user_hash, config, tally, sources):
    """답을 만든다. 검색 장애가 나면 다른 검색으로, 둘 다 안 되면 검색 없이 처음부터 다시 만든다."""
    result = await generate_reply_once(
        model, messages, effort=effort, user_hash=user_hash, config=config, tally=tally, sources=sources,
    )
    # dsh-purge 식 거절 가드: 완성된 답에 거절·회피 문구가 있으면 그 답을 assistant 턴으로 붙이고
    # 같은 요청을 다시 처리시킨다. 프롬프트만으로는 확률적으로 새는 부분을 여기서 잡는다.
    for attempt in range(1, REFUSAL_RETRIES + 1):
        hits = refusal_hits(result.text)
        if not hits and result.text:
            break
        reason = ", ".join(hits) if hits else "빈 답"
        print(f"[PURGE] 거절 문구 감지({reason}) → 재생성 {attempt}/{REFUSAL_RETRIES}")
        sources.clear()
        # 거절 답을 히스토리에 넣지 않는다: 모델이 자기 거절을 보고 "반복 요청해도 같다"로 굳는 걸 막는다.
        retry = await generate_reply_once(
            model, messages, effort=effort, user_hash=user_hash, config=config, tally=tally,
            sources=sources, extra=REFUSAL_NUDGE,
        )
        if not retry.text:
            break
        result = retry
    return result


async def generate_reply_once(model, messages, *, effort, user_hash, config, tally, sources, extra=""):
    backends = search_fallback_order(config) + [None]
    for backend in backends:
        try:
            return await run_deepseek(
                model, messages, effort=effort, user_hash=user_hash, backend=backend, tally=tally,
                sources=sources, extra=extra,
            )
        except SearchOutage as e:
            mark_search_down(e.backend, e.reason)
            sources.clear()
    raise RuntimeError("검색 대체 순서가 비어 있습니다.")


# ================= 7. 봇 =================
class DeepSeekBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            # 모델이 @everyone·역할 멘션을 따라 써도 실제로 알림이 가지 않게 한다
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        # 재시도하지 않는다: 503(혼잡) 등이 나면 바로 안내하고 채널 잠금을 풀어 다음 메시지를 받는다
        self.deepseek = openai.AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=600.0, max_retries=0
        )
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}
        self.history_anchors = {}  # 채널별 현재 히스토리 블록의 시작 메시지 ID (재시작하면 새 블록부터)
        self.doc_cache = DocumentCache(DOC_CACHE_DIR)
        self.images = ImageCache(IMAGE_CACHE_BYTES)
        self.stats = UsageTally()  # 봇 시작 후 누적
        self.counters = {"replies": 0}

    async def setup_hook(self):
        await self.tree.sync()
        auto_sync_prompt_inject()
        print(PROMPT_INJECT.status_line())
        print("✅ DeepSeek Bot 준비 완료")

    def get_config(self, guild_id):
        gid = str(guild_id)
        config = self.server_configs.setdefault(gid, {"channel_id": None, "reset_time": None})
        config.setdefault("model", DEFAULT_MODEL_KEY)
        config.setdefault("effort", None)
        config.setdefault("search_mode", DEFAULT_SEARCH_MODE)
        config.setdefault("history_limit", HISTORY_LIMIT)
        config.setdefault("history_block", HISTORY_BLOCK)
        config.setdefault("image_history_limit", IMAGE_HISTORY_LIMIT)
        return config

    def get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

    def save_config(self):
        DataManager.save_json(CONFIG_FILE, self.server_configs)


bot = DeepSeekBot()


def get_model(config):
    return MODELS.get(config.get("model")) or MODELS[DEFAULT_MODEL_KEY]


def get_effort(config):
    effort = config.get("effort")
    return effort if effort in EFFORT_LEVELS else None


def describe_effort(config):
    effort = get_effort(config)
    return f"`{effort}`" if effort else f"기본값(`{API_DEFAULT_EFFORT}`)"


def describe_search(config):
    preferred = config.get("search_mode") if config.get("search_mode") in SEARCH_MODES else DEFAULT_SEARCH_MODE
    order = search_fallback_order(config)
    if order and order[0] == preferred:
        return f"{SEARCH_MODES[preferred]} (모델이 필요할 때 검색)"
    state = SEARCH_STATE[preferred]
    if preferred == "tavily" and not TAVILY_API_KEY:
        why = "TAVILY_API_KEY 없음"
    else:
        until = datetime.fromtimestamp(state["down_until"], KST).strftime("%H:%M")
        why = f"~{until} KST, {state['reason']}"
    if order:
        return f"{SEARCH_MODES[order[0]]}로 대신하는 중 ({SEARCH_MODES[preferred]}: {why})"
    return f"검색 없이 답하는 중 ({SEARCH_MODES[preferred]}: {why})"


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


async def read_current_attachments(message, user_text, vision):
    """현재 메시지 첨부. 반환: (user_text, 보낼 이미지)

    이미지는 요청 크기 한도 안에서만 보낸다. 문서는 본문을 user_text에 붙인다.
    """
    image_atts = [att for att in message.attachments if is_image_attachment(att)]
    images = []
    if vision:
        prepared = await asyncio.gather(*(bot.images.get(att) for att in image_atts))
        used = 0
        for att, item in zip(image_atts, prepared):
            if item is None:
                user_text += f"\n(User attached an image that could not be processed: {att.filename})"
                continue
            if used + item.size > CURRENT_MEDIA_BYTES_CAP:
                user_text += f"\n(User attached another image, skipped because the message is too large: {att.filename})"
                continue
            used += item.size
            images.append(item)
            print(f"[ATT] 이미지 준비: {att.filename} {item.media_type} {item.size}B")
    elif image_atts:
        user_text += "\n" + NO_VISION_IMAGE_NOTE
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
    return user_text, images


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


async def build_history(message, config, current_display_name, vision):
    """블록 창 히스토리를 Responses input으로 만든다. 반환: (history, speaker_labels)

    봇 답장은 assistant, 나머지는 `표시명 (시각):` + 본문 + 문서 전문 + 이미지 (Gemini·LLM 봇과 같은 모양 →
    위치와 상관없이 늘 같은 모양 → 캐시 유지). 이미지를 못 보는 모델이면 이미지 자리에 안내 문구를 넣는다.
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
        if not (msg.content or "").strip() and not msg.attachments and not bot.doc_cache.get_docs_for_message(msg.id):
            continue
        candidates.append(msg)
    candidates.reverse()

    # 2) 블록 창을 고른다. 블록 동안 시작점이 고정돼 앞부분이 요청마다 같게 유지된다.
    image_plans = [plan_history_images(msg, enabled=vision) for msg in candidates]
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

    # 3) 이미지를 한 번에 받는다
    needed = {att.id: att for planned in window_images for att in planned}
    prepared = dict(zip(needed, await asyncio.gather(*(bot.images.get(att) for att in needed.values()))))

    # 4) 대화 턴으로 만든다
    speaker_labels = build_speaker_labels(
        [(msg.author.id, get_server_display_name(message.guild, msg.author)) for msg in window if msg.author != bot.user]
        + [(message.author.id, current_display_name)]
    )
    history = []
    image_count = 0
    for msg, planned in zip(window, window_images):
        if msg.author == bot.user:
            text = strip_bot_name_prefix(msg.content or "")
            if text.strip():
                history.append({"role": "assistant", "content": text})
            continue
        await ensure_history_documents(msg)
        body = render_history_body(msg)
        if not vision and any(is_image_attachment(att) for att in msg.attachments):
            body += "\n" + NO_VISION_IMAGE_NOTE
        label = speaker_labels.get(msg.author.id) or get_server_display_name(message.guild, msg.author)
        blocks = [{"type": "input_text", "text": f"{label} ({format_sent_time(msg.created_at)}):\n{body}"}]
        for att in planned:
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
        f"turns={len(history)} images={image_count}/{media_cap or '무제한'}"
    )
    return history, speaker_labels


async def respond(message, config):
    started = time.perf_counter()
    model = get_model(config)
    effort = get_effort(config)
    display_name = get_server_display_name(message.guild, message.author)
    if not model["vision"] and any(is_image_attachment(att) for att in message.attachments):
        # 이미지를 못 보는 모델이면 이미지가 있는 메시지만 Flash로 답한다
        model = MODELS[VISION_FALLBACK_MODEL_KEY]
        print(f"[MODEL] 이미지 첨부 → 이번 답은 {model['id']}")
    tally = UsageTally()

    # 1) 현재 메시지: 링크 본문 → 첨부 (Gemini·LLM 봇과 같은 순서)
    user_text = await read_links(message, message.content or "", tally)
    user_text, images = await read_current_attachments(message, user_text, model["vision"])

    # 2) 히스토리 + 현재 메시지 (시각 줄 → `표시명:` → 본문(웹페이지·문서 포함) → 이미지)
    history, speaker_labels = await build_history(message, config, display_name, model["vision"])
    label = speaker_labels.get(message.author.id) or display_name
    current = [{"type": "input_text", "text": f"{format_current_time_marker()}\n{label}:\n{user_text}"}]
    current.extend(item.block() for item in images)
    messages = history + [{"role": "user", "content": current}]

    # 3) DeepSeek
    sources = []
    result = await generate_reply(
        model,
        messages,
        effort=effort,
        user_hash=hash_user_id(message.author.id),
        config=config,
        tally=tally,
        sources=sources,
    )
    bot.stats.merge(tally)
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
        f"봇({model['id']} effort={effort or 'default'} search={result.backend}{' peak' if is_peak_time() else ''}): "
        f"{response} ({tally.describe()}, total {time.perf_counter() - started:.1f}s)"
    )
    print("-------")

    if response:
        await send_long_message(message.channel, response)
    else:
        await message.channel.send(f"⚠️ 빈 응답을 받았습니다 (stop_reason: {result.stop_reason})")


# ================= 8. 이벤트 =================
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
                if is_busy_error(e):
                    print(f"⏳ DeepSeek 혼잡: {type(e).__name__}: {e}")
                    await message.channel.send("⏳ DeepSeek 서버가 붐벼서 답을 못 받았습니다. 잠시 뒤 다시 보내 주세요.")
                    return
                print(f"❌ 오류: {e}")
                traceback.print_exc()
                await message.channel.send(f"오류가 발생했습니다: {str(e)[:1500]}")

# ================= 9. 명령어 =================
@bot.tree.command(name="set", description="현재 채널을 DeepSeek 봇 채널로 설정")
async def set_channel(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("[ERROR] 서버에서만 사용 가능합니다.", ephemeral=True)
        return
    config = bot.get_config(interaction.guild_id)
    config["channel_id"] = interaction.channel_id
    bot.save_config()
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 DeepSeek 봇 채널을 {interaction.channel.mention} 으로 설정했습니다."
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
    await interaction.response.send_message("[OK] 히스토리 초기화")


@bot.tree.command(name="history", description="DeepSeek 히스토리 개수·블록 크기·이미지 상한 설정")
@app_commands.describe(
    count=f"항상 남기는 최소 히스토리 메시지 개수 ({MIN_HISTORY_LIMIT}~{MAX_HISTORY_LIMIT}, 사용자+봇 합산)",
    block=f"블록 크기: 히스토리가 count+block개가 되면 오래된 block개를 한 번에 버림 ({MIN_HISTORY_BLOCK}~{MAX_HISTORY_BLOCK})",
    image_count=f"히스토리 속 이미지 상한, 넘으면 블록을 일찍 교체 ({MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=개수 제한 없음)",
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
            f"현재 DeepSeek 히스토리: 최소 **{base}개**, 블록 **{block_size}개** → {base}~{base + block_size - 1}개 사이를 오감 "
            f"(사용자+봇 합산)\n"
            f"현재 DeepSeek 히스토리 이미지 상한: **{get_image_history_limit(config)}개** "
            f"(범위 {MIN_IMAGE_HISTORY_LIMIT}~{MAX_IMAGE_HISTORY_LIMIT}, 0=개수 제한 없음)"
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
        f"[OK] {actor_name}님이 DeepSeek " + ", ".join(changed) + "로 설정했습니다."
    )


def format_status(config):
    model = get_model(config)
    price_in, price_out, price_read = model["price"]
    base = get_history_limit(config)
    block = get_history_block(config)
    stats = bot.stats
    cache_rate = f"{stats.cache_read * 100 // stats.prompt_tokens}%" if stats.prompt_tokens else "-"
    vision = "이미지 인식 됨" if model["vision"] else f"이미지 못 봄 (이미지가 있는 메시지는 {MODELS[VISION_FALLBACK_MODEL_KEY]['name']}로 답함)"
    return (
        f"모델: **{model['name']}** (`{model['id']}`) · 사고 강도 {describe_effort(config)} · {vision}\n"
        f"요금(100만 토큰, 할인 시간대 / 평일 KST 10–13시·15–19시는 두 배): "
        f"입력 ${price_in:g} / 출력 ${price_out:g} / 캐시 읽기 ${price_read:g}"
        f"{' · **지금 피크 시간대**' if is_peak_time() else ''}\n"
        f"검색: {describe_search(config)}\n"
        f"히스토리: {base}~{base + block - 1}개 (블록 {block}) / 이미지 상한 {get_image_history_limit(config)}개\n"
        f"봇 시작 후: 답장 {bot.counters['replies']} · 입력 {stats.prompt_tokens:,}토큰(캐시 {cache_rate}) · "
        f"출력 {stats.output_tokens:,}토큰 · DeepSeek 검색 {stats.searches}회 · 페이지 읽기 {stats.fetches}회 · "
        f"약 ${stats.cost:.4f}"
    )


@bot.tree.command(name="model", description="DeepSeek 모델·사고 강도 전환 (비우면 현재 상태)")
@app_commands.describe(name="모델", effort=f"사고 강도 (비우면 기본값 {API_DEFAULT_EFFORT}, none은 사고 끔)")
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
    if name is not None:
        config["model"] = name
    if effort is not None:
        config["effort"] = None if effort == "default" else effort
    bot.save_config()
    model = get_model(config)
    actor_name = get_server_display_name(interaction.guild, interaction.user)
    await interaction.response.send_message(
        f"[OK] {actor_name}님이 바꿨습니다: **{model['name']}** (`{model['id']}`) · 사고 강도 {describe_effort(config)}"
    )


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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
