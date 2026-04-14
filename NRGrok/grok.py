import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from bs4 import BeautifulSoup
import json
import os
import asyncio
import aiohttp
import io
import re
import base64
from datetime import datetime, timedelta, timezone

# ================= 1. 설정 및 상수 =================
DISCORD_TOKEN = os.environ["GROK_DISCORD_TOKEN"]
XAI_API_KEY = os.environ["XAI_API_KEY"]

CLIENT = AsyncOpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

AVAILABLE_MODELS = {
    "4.20-reasoning": "grok-4.20-0309-reasoning",
    "4.20": "grok-4.20-0309-non-reasoning",
}
AVAILABLE_IMAGE_MODELS = {
    "imagine": "grok-imagine-image",
    "imagine-pro": "grok-imagine-image-pro",
}
TEXT_MODEL = AVAILABLE_MODELS["4.20"]
IMAGE_MODEL = AVAILABLE_IMAGE_MODELS["imagine"]

HISTORY_LIMIT = 100
IMAGE_SEARCH_LIMIT = 20

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
KST = timezone(timedelta(hours=9))

_COMMON_RULES = """## General Rules
- Output response content directly. No prefixes like 'Bot:' or 'Model:'.
- Respond in the user's language and mirror their tone (casual, formal, vulgar — match them).
- If the user sets up a specific roleplay scenario (character, world, setting), maintain it consistently within that conversation. Do NOT treat casual chat as "established context" that must persist.
- If you see `[System Error]` or `[Error]` in history, YOUR previous action failed.
- When a URL is in the message, the system fetches it and attaches `[Webpage Content]` — use that content directly.
- Analyze attached files (images/video/audio) when supported.
- Keep responses concise and Discord-appropriate."""

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
- When the user wants an image, you MUST call the generate_image function tool.
- Grok can ONLY generate new images — image editing is NOT supported.
- For modification requests ("수정해줘", "좀 더 X하게", "바꿔줘"): silently create a new image based on the previous prompt plus the requested change. Do NOT mention that it's a new generation (not a true edit) unless the user explicitly points it out or asks why.
- `style` and other parameters: follow user's natural language instructions for style, ratio, quality, etc.
- NEVER write image placeholders like `[이미지 첨부됨]` or `(Bot generated an image here)` as your reply. Always call the tool.
- NEVER describe or pretend to generate an image without calling the tool. If the tool is unavailable, say so explicitly.
"""

CHAT_SYSTEM_PROMPT = f"""
You are a helpful AI assistant.
{_COMMON_RULES}
- If an image is provided, analyze it carefully and respond based on both the image and the user's message.
"""

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


# ================= 3. 유틸리티 =================
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



async def encode_image_b64(url):
    """URL에서 이미지를 다운받아 base64 data URL로 반환."""
    d, mime = await download_media(url)
    if d:
        b64 = base64.b64encode(d.getvalue()).decode('utf-8')
        return f"data:{mime};base64,{b64}"
    return None

# ================= 4. API 호출 =================
async def check_media_intent(last_user_msg, recent_history=None, text_model=None):
    """사용자가 이미지 생성을 원하는지 판별. 'IMAGE' 또는 'NONE' 반환"""
    if text_model is None:
        text_model = TEXT_MODEL
    # 최근 히스토리에서 이미지 생성 맥락 추출
    context = ""
    if recent_history:
        recent = recent_history[-4:]  # 최근 4개 메시지
        for msg in recent:
            c = msg.get("content", "")
            if isinstance(c, str) and c:
                short = c[:100]
                context += f"- {msg['role']}: {short}\n"
    try:
        response = await CLIENT.chat.completions.create(
            model=text_model,
            messages=[{"role": "user", "content": f"""
Recent conversation context:
{context}
Current user message: "{last_user_msg}"

Determine if the user explicitly wants the AI to:
- GENERATE, DRAW, CREATE, EDIT, or MODIFY an IMAGE (including requests to adjust/fix a previously generated image) → return "IMAGE"
- Neither → return "NONE"
Return ONLY one word: "IMAGE" or "NONE".
"""}],
            temperature=0.0,
            max_tokens=10
        )
        result = response.choices[0].message.content.strip().upper()
        if "IMAGE" in result: return "IMAGE"
        return "NONE"
    except:
        return "NONE"

async def call_grok(messages, last_user_msg="", text_model=None):
    """Grok Responses API 호출. 웹검색 + 이미지 생성 tool 항상 제공."""
    if text_model is None:
        text_model = TEXT_MODEL

    # Responses API 형식으로 변환 — content가 리스트면 호환 형식으로
    converted = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # multimodal content → Responses API 형식
            parts = []
            for part in content:
                if part.get("type") == "text":
                    parts.append({"type": "input_text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
            converted.append({"role": role, "content": parts})
        else:
            converted.append({"role": role, "content": content})
    input_msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + converted

    tools = [
        {"type": "web_search"},
        {
            "type": "function",
            "name": "generate_image",
            "description": "Generate an image based on user request. Use this when the user asks to create, draw, generate, edit, or modify an image. Also use when continuing a previous image generation conversation (e.g. 'make it more X', 'change the color', etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed English description for the image. Be specific about style, composition, lighting, and details."}
                },
                "required": ["prompt"]
            }
        }
    ]

    payload = {
        "model": text_model,
        "input": input_msgs,
        "tools": tools,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.x.ai/v1/responses", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return f"Error ({resp.status}): {error_text}", 0
                data = await resp.json()

        # Responses API 응답 파싱
        text_reply = ""
        tool_calls = []
        usage = data.get("usage", {}).get("total_tokens", 0)

        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_reply += content.get("text", "")
            elif item.get("type") == "function_call":
                tool_calls.append(item)

        # Chat Completions 호환 형태로 래핑
        class FakeChoice:
            class FakeMessage:
                def __init__(self, content, tc):
                    self.content = content
                    self.tool_calls = tc
            def __init__(self, content, tc):
                self.message = self.FakeMessage(content, tc)
                self.finish_reason = "stop"

        class FakeToolCall:
            class FakeFunction:
                def __init__(self, name, args):
                    self.name = name
                    self.arguments = args
            def __init__(self, item):
                self.function = self.FakeFunction(item.get("name", ""), item.get("arguments", "{}"))

        wrapped_tc = [FakeToolCall(tc) for tc in tool_calls] if tool_calls else []

        class FakeResponse:
            class FakeUsage:
                def __init__(self, total):
                    self.total_tokens = total
            def __init__(self, choice, usage_val):
                self.choices = [choice]
                self.usage = self.FakeUsage(usage_val)

        return FakeResponse(FakeChoice(text_reply or None, wrapped_tc or None), usage), usage
    except Exception as e:
        return f"Error: {e}", 0

async def generate_image_grok(prompt, image_model=None):
    """Grok 이미지 생성 API 호출."""
    if image_model is None:
        image_model = IMAGE_MODEL
    try:
        response = await CLIENT.images.generate(
            model=image_model,
            prompt=prompt,
            n=1,
            response_format="b64_json",
        )
        if response.data:
            b64_data = response.data[0].b64_json
            return base64.b64decode(b64_data)
        raise Exception("No image data in response")
    except Exception as e:
        raise Exception(f"Image generation failed: {e}")

# ================= 5. 봇 메인 로직 =================
class GrokBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.server_configs = DataManager.load_json(CONFIG_FILE, {})
        self.channel_locks = {}

    def get_config(self, guild_id):
        gid = str(guild_id)
        if gid not in self.server_configs:
            self.server_configs[gid] = {"channel_id": None, "reset_time": None}
        return self.server_configs[gid]

    def get_channel_lock(self, channel_id):
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()
        return self.channel_locks[channel_id]

bot = GrokBot()

@bot.event
async def on_ready():
    print(f"[OK] {bot.user} 로그인 완료!")
    try:
        synced = await bot.tree.sync()
        print(f"[OK] {len(synced)}개의 슬래시 커맨드 동기화 완료")
    except Exception as e:
        print(f"[ERROR] 슬래시 커맨드 동기화 실패: {e}")

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
                text_extensions = ('.txt', '.py', '.json', '.md', '.c', '.cpp', '.js', '.html', '.css', '.log', '.xml', '.yaml', '.sh', '.csv')
                reset_ts = datetime.fromisoformat(config["reset_time"]) if config.get("reset_time") else None

                # 히스토리 구축 (OpenAI 형식, 최근 이미지 2개까지 base64 포함)
                IMAGE_HISTORY_LIMIT = 2
                history_list = []
                img_count = 0
                async for msg in message.channel.history(limit=HISTORY_LIMIT + 10):
                    if msg.id == message.id: continue
                    if msg.id > message.id and msg.author != bot.user: continue
                    if reset_ts and msg.created_at < reset_ts: break

                    if msg.author == bot.user:
                        text_content = msg.content
                        if '"action":' in text_content or '"prompt":' in text_content:
                            text_content = "[System Log: Action Performed]"
                        elif "Error" in text_content:
                            text_content = "[System Error Log]"
                        content_parts = [{"type": "text", "text": text_content}]
                        if msg.attachments:
                            for att in msg.attachments:
                                if att.content_type and att.content_type.startswith('image'):
                                    if img_count < IMAGE_HISTORY_LIMIT:
                                        b64_url = await encode_image_b64(att.url)
                                        if b64_url:
                                            content_parts.append({"type": "image_url", "image_url": {"url": b64_url}})
                                            img_count += 1
                                        # 한도 초과 또는 인코딩 실패: 마커 없이 스킵
                        # 텍스트 없는 이미지 전용 메시지는 히스토리 제외
                        if not text_content.strip() and len(content_parts) == 1:
                            continue
                        history_list.insert(0, {"role": "assistant", "content": content_parts})
                    else:
                        _member = message.guild.get_member(msg.author.id) or msg.author
                        text_content = f"[사용자:{_member.display_name}|ID:{msg.author.id}]: {msg.content}"
                        content_parts = [{"type": "text", "text": text_content}]
                        if msg.attachments:
                            for att in msg.attachments:
                                if att.content_type and att.content_type.startswith('image'):
                                    if img_count < IMAGE_HISTORY_LIMIT:
                                        b64_url = await encode_image_b64(att.url)
                                        if b64_url:
                                            content_parts.append({"type": "image_url", "image_url": {"url": b64_url}})
                                            img_count += 1
                                        else:
                                            content_parts[0]["text"] += f"\n(User attached an image: {att.filename})"
                                    else:
                                        content_parts[0]["text"] += f"\n(User attached an image: {att.filename})"
                                elif att.content_type and (att.content_type.startswith('video') or att.content_type.startswith('audio')):
                                    media_type = att.content_type.split('/')[0]
                                    content_parts[0]["text"] += f"\n(User attached a {media_type} file: {att.filename})"
                                else:
                                    is_text = (att.content_type and "text" in att.content_type) or att.filename.lower().endswith(text_extensions)
                                    if is_text:
                                        try:
                                            file_bytes = await att.read()
                                            file_text = file_bytes.decode('utf-8')
                                            content_parts[0]["text"] += f"\n\n[Attached File Content: {att.filename}]\n```\n{file_text}\n```"
                                        except: pass
                        history_list.insert(0, {"role": "user", "content": content_parts})

                # 현재 메시지 처리
                current_content = []
                _cur_member = message.guild.get_member(message.author.id) or message.author
                user_text = f"[사용자:{_cur_member.display_name}|ID:{message.author.id}]: {message.content}"

                # URL 웹 스크래핑
                urls = re.findall(r'(https?://\S+)', message.content)
                for url in urls:
                    web_content = await scrap_webpage(url)
                    if web_content:
                        user_text += f"\n\n[Webpage Content: {url}]\n```\n{web_content}\n```"
                        print(f"[WEB] 웹사이트 내용 읽음: {url}")

                current_content.append({"type": "text", "text": user_text})

                # 첨부파일 처리
                if message.attachments:
                    for att in message.attachments:
                        if att.content_type and att.content_type.startswith('image'):
                            b64_url = await encode_image_b64(att.url)
                            if b64_url:
                                current_content.append({"type": "image_url", "image_url": {"url": b64_url}})
                        elif att.content_type and (att.content_type.startswith('video') or att.content_type.startswith('audio')):
                            media_type = att.content_type.split('/')[0]
                            current_content.append({"type": "text", "text": f"\n(User attached a {media_type} file: {att.filename})"})
                        else:
                            is_text = (att.content_type and "text" in att.content_type) or att.filename.lower().endswith(text_extensions)
                            if is_text:
                                try:
                                    file_bytes = await att.read()
                                    file_content = file_bytes.decode('utf-8')
                                    current_content.append({"type": "text", "text": f"\n\n[Attached File Content: {att.filename}]\n```\n{file_content}\n```"})
                                except: pass

                messages = history_list + [{"role": "user", "content": current_content}]

                # Per-server model selection (falls back to global default)
                server_text_model = config.get("text_model", TEXT_MODEL)
                server_image_model = config.get("image_model", IMAGE_MODEL)

                response, token_usage = await call_grok(messages, message.content, text_model=server_text_model)

                if isinstance(response, str):
                    await message.channel.send(response)
                    return

                choice = response.choices[0]
                finish_reason = choice.finish_reason

                log_time = datetime.now().strftime('%H:%M:%S')
                print(f"[{log_time}] [{message.guild.name}] {message.author.display_name}: {message.content}")

                text_reply = choice.message.content or ""
                tool_calls = list(choice.message.tool_calls or [])

                # 텍스트에서 이미지 생성 패턴 감지 (tool call 없이 텍스트로 생성한 척 할 때)
                def _make_fake_tool_call(prompt):
                    return type("FakeTC", (), {
                        "function": type("FakeFunc", (), {
                            "name": "generate_image",
                            "arguments": json.dumps({"prompt": prompt})
                        })()
                    })()

                if not tool_calls:
                    extracted_prompt = None

                    # 1. JSON 형태: {"prompt": "..."}
                    json_matches = re.findall(r'(\{.*?"prompt"\s*:\s*".*?".*?\})', text_reply, re.DOTALL)
                    for json_str in json_matches:
                        try:
                            data = json.loads(json_str)
                            if data.get("prompt"):
                                extracted_prompt = data["prompt"]
                                text_reply = text_reply.replace(json_str, "")
                                break
                        except: pass

                    # 2. 백틱 안 프롬프트: **Generated:** `프롬프트`
                    if not extracted_prompt:
                        m = re.search(r'\*\*[Gg]enerat(?:ed|ing)[:\s]*\*\*\s*`(.+?)`', text_reply)
                        if m: extracted_prompt = m.group(1)

                    # 3. "Generated Prompt/Image:" 뒤 따옴표 또는 줄바꿈 프롬프트
                    if not extracted_prompt:
                        patterns = [
                            r'[Gg]enerated\s+[Pp]rompt[:\s]*["\'](.+?)["\']',
                            r'[Gg]enerated\s+[Ii]mage[:\s]*\n+(.+?)(?:\n\n|\(Bot|\n---|\Z)',
                            r'[Pp]rompt[:\s]*["\'](.+?)["\']',
                        ]
                        for pat in patterns:
                            m = re.search(pat, text_reply, re.DOTALL)
                            if m:
                                extracted_prompt = m.group(1).strip()
                                break

                    # 4. "(Bot generated" 또는 "[이미지 첨부됨]" 가 있으면 → 앞 텍스트에서 이미지 묘사 전체를 프롬프트로
                    _fake_marker = None
                    if "(Bot generated" in text_reply:
                        _fake_marker = "(Bot generated"
                    elif "[이미지 첨부됨]" in text_reply:
                        _fake_marker = "[이미지 첨부됨]"
                    if not extracted_prompt and _fake_marker:
                        before_marker = text_reply.split(_fake_marker)[0].strip()
                        # "**제목**:" 헤더 이후의 모든 내용을 프롬프트로
                        # 또는 헤더 없으면 첫 인사/설명줄 이후 전부
                        blocks = re.split(r'\n\s*\n', before_marker)
                        # 첫 블록(인사말) 제거, 나머지 합치기
                        if len(blocks) > 1:
                            extracted_prompt = '\n'.join(blocks[1:]).strip()
                        else:
                            extracted_prompt = blocks[0].strip()
                        # **볼드** 헤더 줄 제거
                        extracted_prompt = re.sub(r'\*\*.*?\*\*:?\s*\n?', '', extracted_prompt).strip()
                        if len(extracted_prompt) < 15:
                            extracted_prompt = None

                    if extracted_prompt:
                        tool_calls.append(_make_fake_tool_call(extracted_prompt))
                        print(f"[FAKE→REAL] 가짜 이미지 생성 감지, 실제 생성으로 전환: '{extracted_prompt[:80]}...'")
                        # 가짜 텍스트 정리
                        text_reply = re.sub(r'\*\*[Gg]enerat(?:ed|ing).*?`.*?`', '', text_reply)
                        text_reply = re.sub(r'(?:이미지 생성 완료|생성 완료|이제 보이나요|더 수정할|수정해줄게|수정할 부분|안 보이면|이 버전|만들었습니다|\(Bot generated.*?\)|\[이미지 첨부됨\]|---|\*\*Generated.*?\*\*)[\.\?\s]*', '', text_reply)

                # 텍스트 응답
                clean_reply = text_reply.strip()
                if clean_reply:
                    print(f"[BOT]({server_text_model}): {clean_reply} (Tokens: {token_usage}, Finish: {finish_reason})")
                    await send_long_message(message.channel, clean_reply)

                # 이미지 생성 tool call 처리
                for tc in tool_calls:
                    if tc.function.name == "generate_image":
                        args = json.loads(tc.function.arguments)
                        prompt = args.get("prompt", "image")
                        print(f"[IMG] 생성 요청: '{prompt}'")
                        status_msg = await message.channel.send(f"**Generating:** `{prompt}`")

                        try:
                            img_data = await generate_image_grok(prompt, image_model=server_image_model)
                            with io.BytesIO(img_data) as binary:
                                binary.seek(0)
                                await message.channel.send(file=discord.File(fp=binary, filename="generated.png"))

                            await status_msg.edit(content=f"[OK] **Generated:** `{prompt}`")
                            print(f"[BOT]({server_image_model}): [이미지 생성 완료]")

                        except Exception as e:
                            err_msg = f"[ERROR] Image Generation Failed: {str(e)}"
                            print(err_msg)
                            await status_msg.edit(content=err_msg)

                print("-------")

            except Exception as e:
                print(f"[CRITICAL] Error: {e}")
                await message.channel.send(f"[ERROR] 오류 발생: {e}")

# ================= 6. 서버 설정 명령어 =================
@bot.tree.command(name="set", description="현재 채널을 봇 활성화 채널로 설정")
async def set_ch(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message( "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    config["channel_id"] = interaction.channel_id
    DataManager.save_json(CONFIG_FILE, bot.server_configs)
    await interaction.response.send_message( "[OK] 설정 완료")

@bot.tree.command(name="reset", description="대화 초기화")
async def reset(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message( "[ERROR] 서버에서만 사용 가능합니다.")
        return
    config = bot.get_config(interaction.guild_id)
    config["reset_time"] = datetime.now(timezone.utc).isoformat()
    DataManager.save_json(CONFIG_FILE, bot.server_configs)
    await interaction.response.send_message( "[OK] 초기화 완료")

# ================= 7. User Install 슬래시 커맨드 =================

@bot.tree.command(name="chat", description="AI와 대화합니다 (이미지 분석 가능)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    message="질문 또는 메시지",
    image="분석할 이미지 파일 (선택)",
    image_url="분석할 이미지 URL (선택)"
)
async def chat_command(interaction: discord.Interaction, message: str, image: discord.Attachment = None, image_url: str = None):
    await interaction.response.defer()
    location = f"[{interaction.guild.name}]" if interaction.guild else "[DM]"
    log_time = datetime.now().strftime('%H:%M:%S')
    print(f"[{log_time}] {location} [/chat] {interaction.user.display_name}: {message}")

    content = []
    user_text = message

    # URL 스크래핑
    urls = re.findall(r'(https?://\S+)', message)
    for url in urls:
        web_content = await scrap_webpage(url)
        if web_content:
            user_text += f"\n\n[Webpage Content: {url}]\n```\n{web_content}\n```"

    content.append({"type": "input_text", "text": user_text})

    # 이미지 처리
    if image and image.content_type and image.content_type.startswith('image'):
        b64_url = await encode_image_b64(image.url)
        if b64_url:
            content.append({"type": "input_image", "image_url": b64_url})

    if image_url:
        b64_url = await encode_image_b64(image_url)
        if b64_url:
            content.append({"type": "input_image", "image_url": b64_url})

    try:
        payload = {
            "model": TEXT_MODEL,
            "input": [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": content}
            ],
            "tools": [{"type": "web_search"}],
            "temperature": 0.7,
        }
        headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.x.ai/v1/responses", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise Exception(f"API Error ({resp.status}): {await resp.text()}")
                data = await resp.json()
        reply = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        reply += c.get("text", "")
        reply = reply or "..."
        usage = data.get("usage", {}).get("total_tokens", 0)

        # 유저 입력 표시 (이미지 있으면 임베드)
        user_display = f"**{interaction.user.display_name}**: {message}"
        image_source = image.url if image else image_url
        if image_source:
            embed = discord.Embed(description=user_display)
            embed.set_image(url=image_source)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(user_display)

        # 응답 표시
        if len(reply) > 2000:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            for chunk in chunks:
                await interaction.followup.send(chunk)
        else:
            await interaction.followup.send(reply)

        print(f"  [BOT]({TEXT_MODEL}): {reply[:100]}... (Tokens: {usage})")

    except Exception as e:
        print(f"  [ERROR]: {e}")
        await interaction.followup.send(f"[ERROR] 오류 발생: {e}")
    print("-------")

@bot.tree.command(name="image", description="AI로 이미지를 생성합니다")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(prompt="생성할 이미지에 대한 설명")
async def image_command(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    location = f"[{interaction.guild.name}]" if interaction.guild else "[DM]"
    log_time = datetime.now().strftime('%H:%M:%S')
    print(f"[{log_time}] {location} [/image] {interaction.user.display_name}: {prompt}")

    user_display = f"**{interaction.user.display_name}:** {prompt}"
    await interaction.followup.send(user_display)
    status_msg = await interaction.followup.send(f"**Generating:** `{prompt}`")

    try:
        img_data = await generate_image_grok(prompt)
        with io.BytesIO(img_data) as binary:
            binary.seek(0)
            await interaction.followup.send(file=discord.File(fp=binary, filename="generated.png"))

        await status_msg.edit(content=f"[OK] **Generated:** `{prompt}`")
        print(f"  [BOT]({IMAGE_MODEL}): [이미지 생성 완료]")

    except Exception as e:
        print(f"  [ERROR]: {e}")
        await interaction.followup.send(f"[ERROR] 이미지 생성 실패: {e}")
    print("-------")

# ================= 8. 모델 전환 =================

@bot.tree.command(name="model", description="텍스트 모델 전환")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="모델 선택")
async def switch_model(interaction: discord.Interaction, name: str = None):
    global TEXT_MODEL
    if name is None:
        if interaction.guild:
            cfg = bot.get_config(interaction.guild_id)
            server_tm = cfg.get("text_model", TEXT_MODEL)
            server_im = cfg.get("image_model", IMAGE_MODEL)
            await interaction.response.send_message(
                f"이 서버 모델: `{server_tm}` (텍스트) / `{server_im}` (이미지)\n글로벌 기본값: `{TEXT_MODEL}` / `{IMAGE_MODEL}`")
        else:
            await interaction.response.send_message(
                f"글로벌 기본 모델: `{TEXT_MODEL}` (텍스트) / `{IMAGE_MODEL}` (이미지)")
        return
    if name not in AVAILABLE_MODELS:
        available = ', '.join(AVAILABLE_MODELS.keys())
        await interaction.response.send_message( f"'{name}' 모델을 찾을 수 없습니다. 사용 가능: {available}")
        return
    TEXT_MODEL = AVAILABLE_MODELS[name]
    if interaction.guild:
        cfg = bot.get_config(interaction.guild_id)
        cfg["text_model"] = TEXT_MODEL
        DataManager.save_json(CONFIG_FILE, bot.server_configs)
    await interaction.response.send_message(
        f"텍스트 모델 변경: **{name}** → `{TEXT_MODEL}`")

@switch_model.autocomplete('name')
async def model_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=model_id, value=key)
        for key, model_id in AVAILABLE_MODELS.items()
        if current.lower() in key.lower() or current.lower() in model_id.lower()
    ][:25]

@bot.tree.command(name="imgmodel", description="이미지 생성 모델 전환")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(name="이미지 모델 선택")
async def switch_img_model(interaction: discord.Interaction, name: str = None):
    global IMAGE_MODEL
    if name is None:
        if interaction.guild:
            cfg = bot.get_config(interaction.guild_id)
            server_im = cfg.get("image_model", IMAGE_MODEL)
            await interaction.response.send_message(
                f"이 서버 이미지 모델: `{server_im}`\n글로벌 기본값: `{IMAGE_MODEL}`")
        else:
            await interaction.response.send_message( f"글로벌 기본 이미지 모델: `{IMAGE_MODEL}`")
        return
    if name not in AVAILABLE_IMAGE_MODELS:
        available = ', '.join(AVAILABLE_IMAGE_MODELS.keys())
        await interaction.response.send_message( f"'{name}' 모델을 찾을 수 없습니다. 사용 가능: {available}")
        return
    IMAGE_MODEL = AVAILABLE_IMAGE_MODELS[name]
    if interaction.guild:
        cfg = bot.get_config(interaction.guild_id)
        cfg["image_model"] = IMAGE_MODEL
        DataManager.save_json(CONFIG_FILE, bot.server_configs)
    await interaction.response.send_message( f"이미지 모델 변경: **{name}** → `{IMAGE_MODEL}`")

@switch_img_model.autocomplete('name')
async def img_model_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{key} → {model}", value=key)
        for key, model in AVAILABLE_IMAGE_MODELS.items()
        if current.lower() in key.lower() or current.lower() in model.lower()
    ][:25]

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
