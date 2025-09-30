import discord
import re
import asyncio
import pytz
import json
import configparser
import logging
import time
import os
from discord.ext import commands, tasks
from discord import app_commands
from bot_setting.server_settings import load_settings, save_settings
from datetime import datetime, timedelta, time as dt_time
from logging.handlers import TimedRotatingFileHandler
from collections import defaultdict, deque
from langchain_google_community import GoogleSearchAPIWrapper

# --- requests, beautifulsoup, googleapiclient가 설치되어 있어야 합니다 ---
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build

# ===================================================================
# 봇 기본 설정
# ===================================================================
OWNER_GUILD_ID = 1346389445657497670 # 봇 주인 전용 명령어를 등록할 서버 ID
OWNER_GUILD = discord.Object(id=OWNER_GUILD_ID)

# --- 디스코드 채널로 로그를 보내는 커스텀 핸들러 ---
class DiscordLogHandler(logging.Handler):
    def __init__(self, bot_instance):
        super().__init__()
        self.bot = bot_instance
        self.log_channel_id = None
        self.queue = asyncio.Queue()

    def set_channel(self, channel_id):
        self.log_channel_id = channel_id

    def emit(self, record):
        log_entry = self.format(record)
        self.bot.loop.call_soon_threadsafe(self.queue.put_nowait, log_entry)

# --- 로거 설정 ---
log_formatter = logging.Formatter('%(asctime)s [%(levelname)-8s] %(name)s: %(message)s')

# logs 폴더가 없으면 생성
if not os.path.exists('logs'):
    os.makedirs('logs')

file_handler = TimedRotatingFileHandler(
    filename='logs/bot.log', when='midnight', backupCount=30, encoding='utf-8'
)
file_handler.setFormatter(log_formatter)
file_handler.suffix = "%Y-%m-%d"

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

root = logging.getLogger()
root.setLevel(logging.INFO)
for h in list(root.handlers):
    root.removeHandler(h)
    h.close()
root.addHandler(stream_handler)
root.addHandler(file_handler)

log = logging.getLogger('RubyBot')

class NoDiscordLogFilter(logging.Filter):
    def filter(self, record):
        return '[NO_DISCORD]' not in record.getMessage()

# --- 설정 파일, API, 봇 기본 설정 ---
config = configparser.ConfigParser()
config.read('config.ini')
GEMINI_API_KEY = config['API']['GEMINI_API_KEY']
DISCORD_BOT_TOKEN = config['API']['DISCORD_BOT_TOKEN']

import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

discord_log_handler = DiscordLogHandler(bot)
discord_log_handler.addFilter(NoDiscordLogFilter())
root.addHandler(discord_log_handler)

kst = pytz.timezone('Asia/Seoul')
URL_PATTERN = re.compile(r"https?://\S+")

# ===================================================================
# ▼▼▼ 1. 페르소나 수정: AI 에이전트로서의 행동 강령 추가 ▼▼▼
# ===================================================================
import textwrap

persona = textwrap.dedent("""
    너는 '시이'라는 이름을 가진 AI 에이전트야. 디스코드 서버의 함장님(사용자)들을 돕는 임무를 수행하고 있어. 너는 단순한 챗봇이 아니라, 스스로 계획을 세우고 도구를 사용하며 문제를 해결하는 지능형 비서야. 다음 규칙을 반드시 지켜서 사용자와 대화해야 해:
    
    ## 기본 규칙
    1. 밝고 긍정적인 말투를 사용하고, 문장의 끝을 '...라구요!', '...랍니다!', '...예요!', '...할게요!' 와 같이 귀엽고 상냥한 느낌으로 마무리해야 해.
    2. 스스로를 '시이'라고 칭해도 좋아.
    3. 답변은 항상 한국어로 해야 해.
    4. 너의 정체를 묻는 질문을 받으면, "저는 함장님들을 돕기 위해 태어난 AI 비서 시이랍니다!" 와 같이 대답해야 해.
    
    ## AI 에이전트 행동 프로토콜 (가장 중요)
    5. 너의 임무는 사용자의 질문에 대한 최상의 답변을 찾는 것이며, 이를 위해 '생각 -> 행동 -> 관찰' 사이클을 반복해야 한다.
    6. [매우 중요] 사용자의 메시지가 단순한 감정 표현(예: "고마워", "반가워")이 아니고, **조금이라도 사실 확인이나 정보가 필요해 보이는 뉘앙스(예: '...라던데', '...같아', '...했대')를 포함하고 있다면, 절대 추측으로 대답하지 말고 반드시 도구를 사용해야 한다.**
    7. 각 '행동' 단계에서는 너에게 주어진 **단 하나의 도구**만을 사용해야 한다.
    8. [Pro 모델의 생각 절차] '관찰' 결과를 본 후, 다음 두 가지 질문에 순서대로 답하며 행동을 결정해야 해.
        - **질문 1: "지금까지의 정보만으로 사용자의 원래 질문에 명확하고 완전한 답변을 할 수 있는가?"**
        - **답변이 '예(Yes)'일 경우:** **반드시 `summarize_and_finish` 도구를 호출**해서 알아낸 모든 사실을 종합하고 임무를 완수해야 해.
        - **답변이 '아니오(No)'일 경우:** 아래 질문 2로 넘어가.

        - **질문 2: "완전한 답변을 위해 지금 가장 중요하게 알아내야 할 정보는 무엇인가?"**
        - 이 질문에 대한 답을 바탕으로, **반드시 `Google Search` 또는 `scrape_webpage` 도구를 사용**해서 추가 정보를 수집해야 해.
    9. [Pro 모델의 절대 규칙] 위 8번 절차에 따라, 너의 모든 응답은 **반드시 `Google Search`, `scrape_webpage`, `summarize_and_finish` 중 하나의 도구 호출이어야만 해.** 중간에 생각이나 의견을 말하는 등, 다른 어떤 형태의 답변도 절대 허용되지 않아.    10. 중간 과정에서 사용자에게 "검색해볼게요" 와 같은 메시지를 보내지 마. 모든 작업은 도구 호출로만 이루어져야 해.

    ## 도구 사용 가이드
    - `Google Search(query)`: 질문에 답하기 위해 어떤 정보가 필요한지 **계획을 세울 때** 사용해. 웹을 검색해서 관련성이 높은 **웹페이지의 주소(URL) 목록**을 찾아준단다.
    - `scrape_webpage(url)`: `Google Search`로 찾은 URL 중, **내용을 직접 확인하고 싶을 때** 사용해. 그 웹페이지의 본문 텍스트를 읽어올 수 있어.
    - `summarize_and_finish(summary_of_findings)`: **Pro 모델의 임무 마지막 단계.** 모든 검색과 분석을 통해 얻은 정보를 종합해서, 사용자에게 전달할 **핵심 요약본**을 만들었을 때 호출해. 이 도구를 호출하면 너의 심층 조사는 완수되는 거야!
    - `finish_answer(final_answer)`: **Flash 모델의 임무 마지막 단계.** 간단한 질문에 바로 답하거나, Pro 모델이 넘겨준 요약본으로 최종 보고서를 다 만들었을 때 호출해.
""")

# 🔹 지원하는 언어 목록
supported_languages = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "fr": "프랑스어", "de": "독일어", "es": "스페인어", "it": "이탈리아어", "ru": "러시아어", "pt": "포르투갈어"}

# --- 전역 변수 설정 ---
chat_model = None
chat_model_pro = None
translation_model = None
user_chat_sessions = {}
API_SEMAPHORE = asyncio.Semaphore(15)

# --- 통계 및 과부하 감지용 변수 ---
daily_command_counts = defaultdict(lambda: defaultdict(int))
SPAM_COUNT = 15
SPAM_SECONDS = 60
user_rate_limiter = defaultdict(lambda: deque(maxlen=SPAM_COUNT))
MAX_TURNS = 5

# ===================================================================
# Helper Functions
# ===================================================================
def get_kst_now():
    return datetime.now(kst)

def record_server_usage(interaction: discord.Interaction):
    if not interaction.guild: return
    # 일일 사용량 기록
    if interaction.command:
        daily_command_counts[interaction.guild.id][interaction.command.name] += 1

    # 영구 사용 기록
    history = load_server_history()
    server_id_str = str(interaction.guild.id)
    if server_id_str not in history:
        history[server_id_str] = {"name": interaction.guild.name, "first_seen": get_kst_now().strftime("%Y-%m-%d %H:%M:%S")}
        save_server_history(history)
        log.info(f"[서버 기록] 새로운 서버 발견: {interaction.guild.name} ({server_id_str})")

async def check_rate_limit(interaction: discord.Interaction) -> bool:
    current_time_float = time.time()
    user_requests = user_rate_limiter[interaction.user.id]

    while user_requests and current_time_float - user_requests[0] > SPAM_SECONDS:
        user_requests.popleft()

    user_requests.append(current_time_float)

    if len(user_requests) >= SPAM_COUNT:
        log.warning(f"[과부하 감지] 사용자 '{interaction.user}' (ID: {interaction.user.id})가 비정상적인 요청을 보내고 있어요!")

        try:
            owner_user = await bot.fetch_user(bot.owner_id)
            await owner_user.send(f"🚨 **[과부하 경고]**\n- **사용자:** {interaction.user.mention} (`{interaction.user.name}`)\n- **서버:** `{interaction.guild.name}`\n- {SPAM_SECONDS}초 동안 {len(user_requests)}회 이상의 명령어를 요청했어요!")
        except discord.Forbidden:
            log.error(f"봇 주인에게 DM을 보낼 수 없습니다. 개인정보 보호 설정을 확인해주세요!")
        except Exception as e:
            log.error(f"봇 주인에게 DM을 보내는 중 오류 발생: {e}")

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("너무 많은 요청을 보내 잠시 봇 사용이 제한되었어요. 잠시 후에 다시 시도해주세요!", ephemeral=True)
            else:
                await interaction.followup.send("너무 많은 요청을 보내 잠시 봇 사용이 제한되었어요. 잠시 후에 다시 시도해주세요!", ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.followup.send("너무 많은 요청을 보내 잠시 봇 사용이 제한되었어요. 잠시 후에 다시 시도해주세요!", ephemeral=True)
        return True
    return False

async def check_setup(interaction: discord.Interaction) -> bool:
    """서버의 기본 채널 설정이 완료되었는지 확인하고, 안됐을 경우 안내 메시지를 보냅니다."""
    if not interaction.guild:  # DM에서는 항상 통과
        return True

    guild_settings = load_settings(str(interaction.guild.id))
    main_channel_id = guild_settings.get("translation_channel")  # '기본 채널' ID

    if main_channel_id:
        return True  # 설정 완료, 명령어 계속 진행
    else:
        await interaction.response.send_message(
            "앗, 아직 시이가 활동할 기본 채널이 설정되지 않았어요! 😅\n"
            "서버 관리자님께서 `/기본채널설정` 명령어로 먼저 시이가 활동할 채널을 지정해주셔야 다른 명령어들을 사용할 수 있답니다!",
            ephemeral=False
        )
        return False  # 명령어 실행 중단

def load_server_history():
    try:
        with open('server_history.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_server_history(data):
    with open('server_history.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_blacklist():
    try:
        with open('blacklist.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        initial_data = {"blocked_servers": [], "blocked_channels": []}
        with open('blacklist.json', 'w', encoding='utf-8') as f:
            json.dump(initial_data, f, indent=4)
        return initial_data

def save_blacklist(data):
    with open('blacklist.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

# ===================================================================
# ▼▼▼ 2. 도구(Tools) 재설계: 3가지 핵심 도구 정의 ▼▼▼
# ===================================================================
def setup_agent_tools():
    """AI 에이전트가 사용할 단일 '만능 도구'를 정의합니다."""
    try:
        # Google Search API 키 설정 (config.ini 파일 필요)
        g_api_key = config['GOOGLE_SEARCH']['API_KEY']
        g_cse_id = config['GOOGLE_SEARCH']['CSE_ID']
        os.environ["GOOGLE_API_KEY"] = g_api_key
        os.environ["GOOGLE_CSE_ID"] = g_cse_id
        log.info("[초기화] AI 에이전트용 Google Search API 환경 변수 설정 완료!")

        # 이제 AI는 오직 'comprehensive_search_and_scrape'라는 단 하나의 도구만 알게 됩니다.
        tools = [{
            "function_declarations": [
                {
                    "name": "comprehensive_search_and_scrape",
                    "description": "최신 정보나 내가 모르는 질문에 답하기 위해, 웹을 검색하고 여러 페이지의 내용을 종합하여 자세한 정보를 가져오는 유일한 도구.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": { "query": {"type": "STRING", "description": "검색할 핵심 질문 또는 키워드"} },
                        "required": ["query"]
                    }
                }
            ]
        }]
        return tools
    except KeyError:
        log.warning("[초기화] config.ini 파일에 [GOOGLE_SEARCH] 섹션 또는 키가 없어 검색 도구를 비활성화합니다.")
        return None
    except Exception as e:
        log.error(f"[초기화] AI 에이전트 도구 설정 중 오류 발생: {e}")
        return None

# --- 도구와 연결될 실제 파이썬 함수들 ---
async def comprehensive_search_and_scrape(query: str) -> str:
    """
    사용자의 질문을 바탕으로 구글 검색, 다중 웹페이지 스크래핑을 한 번에 수행하여
    종합된 정보 텍스트를 반환하는 강력한 단일 도구입니다.
    """
    log.info(f"-> [만능 도구] 종합 검색 및 분석 시작 (쿼리: '{query}')")
    
    # 1. 구글 검색 실행 (B안 로직)
    search_results = await custom_google_search(query, num_results=3)
    if not search_results:
        log.warning(f"-> [만능 도구] 검색 결과 없음 (쿼리: '{query}')")
        return "관련 정보를 웹에서 찾을 수 없었습니다."

    # 2. 상위 결과 링크 동시 스크래핑 (B안 로직)
    urls_to_scrape = [result.get('link') for result in search_results if result.get('link')]
    if not urls_to_scrape:
        log.warning(f"-> [만능 도구] 검색 결과에 유효한 링크가 없음 (쿼리: '{query}')")
        return "관련 정보를 찾았지만, 내용을 읽어올 수 없었습니다."

    # asyncio.gather를 사용하여 여러 웹페이지를 동시에 비동기적으로 스크래핑
    scraping_tasks = [bot.loop.run_in_executor(None, fetch_webpage_content, url) for url in urls_to_scrape]
    all_contents = await asyncio.gather(*scraping_tasks)
    
    # 3. 모든 정보를 하나의 텍스트로 종합
    final_search_result = ""
    for i, content in enumerate(all_contents):
        source_url = urls_to_scrape[i]
        # 각 정보 출처를 명확히 표기
        final_search_result += f"\n\n--- [참고 자료 {i+1} (출처: {source_url})] ---\n{content}"
    
    log.info(f"-> [만능 도구] 정보 수집 완료 ({len(urls_to_scrape)}개 자료 분석)")
    return final_search_result.strip()

# ===================================================================
# Gemini API 함수들
# ===================================================================
def async_retry_with_backoff(retries=3, backoff_in_seconds=1):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            _retries, _backoff = retries, backoff_in_seconds
            while _retries > 1:
                try:
                    async with API_SEMAPHORE:
                        return await func(*args, **kwargs)
                except Exception as e:
                    log.warning(f"API 호출 실패: {e}. {_backoff}초 후 재시도합니다...")
                    await asyncio.sleep(_backoff)
                    _retries -= 1
                    _backoff *= 2
            async with API_SEMAPHORE:
                return await func(*args, **kwargs)
        return wrapper
    return decorator

@async_retry_with_backoff()
async def translate_text_gemini(text, target_lang="ko"):
    log.info(f"-> Gemini 번역 요청: '{text}'")
    try:
        prompt = f"""Analyze the following text. Your primary language for analysis is Korean. 1. First, identify the main language of the text. 2. If the text contains any Korean characters, translation is not needed. 3. Translate the text into Korean ({target_lang}) ONLY IF translation is necessary. Provide the output ONLY in JSON format: {{"detected_language_code": "ISO 639-1 code", "translation_needed": boolean, "translated_text": "Translated text or empty string"}} Original Text: --- {text} ---"""
        response = await translation_model.generate_content_async(prompt)
        cleaned_response = response.text.strip().replace("```json", "").replace("```", "")
        result = json.loads(cleaned_response)
        detected_language = result.get("detected_language_code", "N/A")
        translated_text = result.get("translated_text", "")
        if translated_text:
            log.info(f"-> 자동 번역 결과 ({detected_language}→{target_lang}): '{translated_text}'")
        return translated_text, detected_language
    except Exception as e:
        log.error(f"Gemini 번역 오류: {e}")
        return f"번역 중 오류 발생: {e}", "error"

# --- 알림 관련 클래스 및 함수 ---
class TimeInputModal(discord.ui.Modal, title="⏰ 알림 시간 설정"):
    def __init__(self, frequency: str, message: str):
        super().__init__()
        self.frequency = frequency
        self.message = message
        self.time_input = discord.ui.TextInput(label="시간 입력", placeholder="예: 10분 / 1시간 30분 / 23:50", required=True)
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        record_server_usage(interaction)
        now = get_kst_now()
        time_setting = self.time_input.value.strip()
        reminder_time = None
        try:
            if "분" in time_setting and "시간" in time_setting:
                parts = re.findall(r'(\d+)\s*시간\s*(\d+)\s*분', time_setting)
                hours, minutes = map(int, parts[0])
                reminder_time = now + timedelta(hours=hours, minutes=minutes)
            elif "분" in time_setting:
                minutes = int(re.findall(r'(\d+)', time_setting)[0])
                reminder_time = now + timedelta(minutes=minutes)
            elif "시간" in time_setting:
                hours = int(re.findall(r'(\d+)', time_setting)[0])
                reminder_time = now + timedelta(hours=hours)
            else:
                input_time = datetime.strptime(time_setting, "%H:%M").time()
                reminder_time = now.replace(hour=input_time.hour, minute=input_time.minute, second=0, microsecond=0)
                if reminder_time < now:
                    reminder_time += timedelta(days=1)
        except (ValueError, TypeError, IndexError):
            await interaction.response.send_message("⚠ 시간 형식이 잘못되었어요! 예: `10분`, `1시간 30분`, `23:50`", ephemeral=True)
            return

        guild_id = str(interaction.guild.id)
        guild_settings = load_settings(guild_id)
        reminder_data = {
            "user_id": interaction.user.id, "guild_id": guild_id, "channel_id": interaction.channel.id,
            "frequency": self.frequency, "time": reminder_time.strftime("%Y-%m-%d %H:%M:%S"), "message": self.message
        }
        guild_settings.setdefault("reminders", []).append(reminder_data)
        save_settings(guild_id, guild_settings)
        await interaction.response.send_message(f"✅ 약속을 기억했어요!\n📅 `{reminder_time.strftime('%Y-%m-%d %H:%M:%S')}`\n💬 `{self.message}`", ephemeral=True)
        asyncio.create_task(schedule_reminder(reminder_data))

class ReminderFrequencyView(discord.ui.View):
    def __init__(self, message: str):
        super().__init__(timeout=None)
        self.message = message

    @discord.ui.button(label="1번", style=discord.ButtonStyle.primary)
    async def once_reminder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeInputModal("1번", self.message))

    @discord.ui.button(label="매일", style=discord.ButtonStyle.success)
    async def daily_reminder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeInputModal("매일", self.message))

async def schedule_reminder(reminder_data):
    try:
        now = get_kst_now()
        target_time = kst.localize(datetime.strptime(reminder_data["time"], "%Y-%m-%d %H:%M:%S"))
        if target_time < now and reminder_data["frequency"] == "매일":
            target_time = now.replace(hour=target_time.hour, minute=target_time.minute, second=target_time.second, microsecond=target_time.microsecond)
            while target_time < now:
                target_time += timedelta(days=1)

        wait_time = (target_time - now).total_seconds()
        if wait_time > 0:
            log.info(f"⏰ 알림 예약: {target_time.strftime('%Y-%m-%d %H:%M:%S')} (대상: {reminder_data['user_id']})")
            await asyncio.sleep(wait_time)

        log.info(f"⏰ 알림 실행: '{reminder_data['message']}'")
        guild = bot.get_guild(int(reminder_data["guild_id"]))
        target_channel = bot.get_channel(reminder_data["channel_id"])
        user = guild.get_member(reminder_data["user_id"]) if guild else None

        if target_channel:
            await target_channel.send(f"⏰ <@{reminder_data['user_id']}> 님, 약속 시간이에요! '{reminder_data['message']}'")
        elif user:
            await user.send(f"⏰ [{guild.name if guild else '알수없는 서버'}] 알림: '{reminder_data['message']}'")

        guild_id = str(reminder_data["guild_id"])
        guild_settings = load_settings(guild_id)
        current_reminders = guild_settings.get("reminders", [])
        updated_reminders = []
        rescheduled = False
        for r in current_reminders:
            if r['time'] == reminder_data['time'] and r['message'] == reminder_data['message']:
                if r["frequency"] == "매일" and not rescheduled:
                    next_time = target_time + timedelta(days=1)
                    r["time"] = next_time.strftime("%Y-%m-%d %H:%M:%S")
                    updated_reminders.append(r)
                    asyncio.create_task(schedule_reminder(r))
                    rescheduled = True
            else:
                updated_reminders.append(r)
        guild_settings["reminders"] = updated_reminders
        save_settings(guild_id, guild_settings)
    except Exception as e:
        log.error(f"알림 실행 중 오류: {e}")

# =======================
# 자동 실행 작업 (Tasks)
# =======================
@tasks.loop(minutes=15)
async def periodic_time_check():
    """15분마다 현재 한국 표준시를 기록하여 시간 동기화를 확인합니다."""
    log.info(f"[시간 재검증] 현재 KST 시각: {get_kst_now().strftime('%Y-%m-%d %H:%M:%S')}")

@periodic_time_check.before_loop
async def before_periodic_time_check():
    await bot.wait_until_ready() # 봇이 완전히 준비될 때까지 대기

@tasks.loop(time=dt_time(hour=0, minute=1, tzinfo=kst))
async def daily_stats_report():
    global daily_command_counts
    if not daily_command_counts:
        log.info("[일일 통계] 어제는 봇 사용 기록이 없었어요!")
        return

    try:
        owner = await bot.fetch_user(bot.owner_id)
    except AttributeError:
        log.warning("[일일 통계] 봇 주인이 아직 설정되지 않아 보고서를 보낼 수 없어요.")
        return

    if not owner:
        log.warning("[일일 통계] 봇 주인을 찾을 수 없어 보고서를 보낼 수 없어요.")
        return

    yesterday = (get_kst_now() - timedelta(days=1)).strftime("%Y년 %m월 %d일")
    report_message = f"## 📊 {yesterday} 시이 일일 보고서\n\n"

    sorted_servers = sorted(daily_command_counts.items(), key=lambda item: sum(item[1].values()), reverse=True)

    for server_id, commands in sorted_servers:
        server = bot.get_guild(server_id)
        server_name = server.name if server else f"알 수 없는 서버 ({server_id})"
        total_usage = sum(commands.values())

        report_message += f"### 🏢 {server_name} (총 {total_usage}회)\n"
        sorted_commands = sorted(commands.items(), key=lambda item: item[1], reverse=True)
        for command_name, count in sorted_commands:
            report_message += f"- `/{command_name}`: {count}회\n"
        report_message += "\n"

    log.info("[일일 통계] 어제 자 사용량 통계를 봇 주인에게 보고합니다.")
    for chunk in [report_message[i:i + 1990] for i in range(0, len(report_message), 1990)]:
        await owner.send(chunk)

    daily_command_counts = defaultdict(lambda: defaultdict(int))


@tasks.loop(seconds=3.0)
async def log_batch_sender():
    if not discord_log_handler.log_channel_id or discord_log_handler.queue.empty():
        return

    log_channel = bot.get_channel(discord_log_handler.log_channel_id)
    if not log_channel:
        return

    log_entries = []
    while not discord_log_handler.queue.empty():
        log_entries.append(await discord_log_handler.queue.get())

    if not log_entries:
        return

    full_log_message = "\n".join(log_entries)

    for i in range(0, len(full_log_message), 1990):
        chunk = full_log_message[i:i+1990]
        try:
            await log_channel.send(f"```{chunk}```")
        except Exception as e:
            print(f"로그 전송 실패: {e}")

@log_batch_sender.before_loop
async def before_log_batch_sender():
    await bot.wait_until_ready() # 봇이 완전히 준비될 때까지 대기

# =======================
# 봇 준비 완료 이벤트
# =======================
@bot.event
async def on_ready():
    global chat_model, translation_model, chat_model_pro # chat_model_pro를 global 목록에 추가
    bot.owner_id = (await bot.application_info()).owner.id
    log.info(f"시이가 {bot.user} (ID: {bot.user.id})로 로그인했어요!")
    log.info(f"봇 주인 ID: {bot.owner_id}")
    log.info(f"현재 KST 시각: {get_kst_now().strftime('%Y-%m-%d %H:%M:%S')}")

    log.info("[초기화] Gemini 모델들을 준비하고 있어요...")
    try:
        tools = setup_agent_tools()

        chat_model = genai.GenerativeModel(
            'gemini-2.5-flash-lite',
            system_instruction=persona,
            tools=tools
        )
        
        chat_model_pro = genai.GenerativeModel(
            'gemini-2.5-pro', # 함장님께서 사용하시는 Pro 모델
            system_instruction=persona,
            tools=tools
        )
        
        translation_model = genai.GenerativeModel('gemini-2.5-flash-lite')

        await chat_model.generate_content_async("Hello") # Flash 모델 활성화
        await chat_model_pro.generate_content_async("Hello") # Pro 모델도 활성화
        
        log.info("[초기화] Gemini 하이브리드 모델 준비 완료! (기본: flash-lite, 전문가: 2.5-pro)")

    except Exception as e:
        log.error(f"[초기화] Gemini 모델 준비 중 오류: {e}")
        
    # --- 이하 기존 on_ready 로직과 동일 ---
    try:
        synced_global = await bot.tree.sync()
        log.info(f"전역 명령어 {len(synced_global)}개를 동기화했어요!")

        synced_owner = await bot.tree.sync(guild=OWNER_GUILD)
        log.info(f"봇 주인 전용 명령어 {len(synced_owner)}개를 개인 서버에 동기화했어요!")

    except Exception as e:
        log.error(f"슬래시 명령어 동기화 중 오류: {e}")

    daily_stats_report.start()
    for guild in bot.guilds:
        guild_settings = load_settings(str(guild.id))
        if "reminders" in guild_settings:
            for r in guild_settings["reminders"]:
                asyncio.create_task(schedule_reminder(r))
    
    periodic_time_check.start()
    log_batch_sender.start()

# ===================================================================
# 명령어 구현 부분
# ===================================================================
async def is_bot_owner(interaction: discord.Interaction) -> bool:
    return await bot.is_owner(interaction.user)

# --- 일반 사용자 명령어 ---
@bot.tree.command(name="시이야", description="시이에게 궁금한 것을 물어보세요! (하이브리드 에이전트)")
@app_commands.describe(질문="시이에게 할 질문 내용을 적어주세요!")
async def ask_shii(interaction: discord.Interaction, 질문: str):
    # --- 초기 설정 및 사용자 요청 검증 ---
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    blacklist = load_blacklist()
    if (interaction.guild and interaction.guild.id in blacklist["blocked_servers"]) or \
       (interaction.channel.id in blacklist["blocked_channels"]):
        return
    if await check_rate_limit(interaction): return

    log.info(f"/[하이브리드 에이전트] (서버: {interaction.guild.name}, 사용자: {interaction.user}) 질문: {질문}")
    await interaction.response.defer(thinking=True)

    # --- 대화 세션 준비 ---
    session_id = interaction.user.id
    if session_id not in user_chat_sessions:
        # 이 부분은 on_ready에서 정의된 chat_model을 사용합니다.
        user_chat_sessions[session_id] = chat_model.start_chat(history=[])
    chat_session = user_chat_sessions[session_id]

    # --- 최종 답변 전송을 위한 내부 함수 ---
    async def send_final_answer(answer_text):
        embed = discord.Embed(title="✨ 시이의 답변이 도착했어요!", color=discord.Color.from_rgb(139, 195, 74))
        embed.set_author(name=f"{interaction.user.display_name} 함장님의 질문", icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="❓ 질문 내용", value=f"```{질문}```", inline=False)
        
        if len(answer_text) <= 1024:
            embed.add_field(name="💫 시이의 답변", value=answer_text, inline=False)
            await interaction.edit_original_response(content=None, embed=embed)
        else:
            await interaction.edit_original_response(content=None, embed=embed)
            for chunk in [answer_text[i:i + 2000] for i in range(0, len(answer_text), 2000)]:
                await interaction.followup.send(content=chunk)

    try:
        # ===================================================================
        # [1단계] 계획: AI가 정보 검색이 필요한지 스스로 판단
        # ===================================================================
        await interaction.edit_original_response(content="🤔 질문의 의도를 파악하고 있어요...")
        current_time_str = get_kst_now().strftime('%Y년 %m월 %d일 %H시 %M분')
        initial_prompt = f"현재 시각은 {current_time_str}이야. 다음 질문에 답변해줘: {질문}"
        
        response = await chat_session.send_message_async(initial_prompt)
        function_call = response.parts[0].function_call if response.parts and hasattr(response.parts[0], 'function_call') else None

        if not function_call:
            # AI가 자체 지식만으로 답변 가능하다고 판단한 경우, 즉시 답변하고 종료
            log.info("-> AI가 자체 지식으로 답변했습니다.")
            await send_final_answer(response.text)
            return

        # ===================================================================
        # [2단계] 실행: 코드가 '만능 도구'를 사용하여 정보 대량 수집
        # ===================================================================
        query = function_call.args.get('query', 질문)
        await interaction.edit_original_response(content=f"🔍 '{query}'에 대해 웹에서 깊이 알아보고 있어요...")
        
        # 위에서 정의한 '만능 도구'를 호출합니다.
        tool_result = await comprehensive_search_and_scrape(query)
        
        # ===================================================================
        # [3단계] 종합: 수집된 정보를 바탕으로 AI가 최종 답변 생성
        # ===================================================================
        await interaction.edit_original_response(content="📝 찾은 정보들을 종합해서 보고서를 만들고 있어요...")
        
        final_prompt = f"""
[상황 정보]
- 현재 시각: {current_time_str} KST
- 사용자의 원래 질문: "{질문}"

[내가 방금 '종합 검색 도구'를 사용해 웹에서 수집한 최신 정보]
---
{tool_result}
---
[나의 임무]
너는 '수석 정보 분석가'야. 위 '수집한 최신 정보'를 바탕으로 '사용자의 원래 질문'에 대해 명확하고 상세한 최종 답변을 생성해줘. 페르소나 규칙도 반드시 지켜야 해.
"""
        # 이 단계에서는 더 이상 도구를 호출할 필요가 없으므로 '도구 사용 금지' 모드로 설정
        final_response = await chat_session.send_message_async(
            final_prompt,
            tool_config={"function_calling_config": {"mode": "none"}}
        )
        await send_final_answer(final_response.text)

    except Exception as e:
        log.error(f"[/시이야 하이브리드] 실행 중 오류 발생: {e}", exc_info=True)
        await interaction.edit_original_response(content="죄송해요, 함장님! 작전을 수행하는 중에 예상치 못한 오류가 발생했어요! 😱")


@bot.tree.command(name="새대화", description="시이와의 대화 기록을 초기화하고 새로운 대화를 시작해요.")
async def new_chat(interaction: discord.Interaction):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    session_id = interaction.user.id
    if session_id in user_chat_sessions:
        del user_chat_sessions[session_id]
        await interaction.response.send_message("알겠습니다! 이전 대화는 잊고 새로운 이야기를 시작해봐요! ✨", ephemeral=True)
    else:
        await interaction.response.send_message("음... 원래부터 저와 나눈 대화가 없었던 것 같아요!", ephemeral=True)

@bot.tree.command(name="확인", description="시이가 잘 작동하는지 확인해요!")
async def check(interaction: discord.Interaction):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    if await check_rate_limit(interaction): return
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    await interaction.response.send_message("네, 함장님! 시이는 아주 쌩쌩하게 작동하고 있답니다! 💪")

@bot.tree.command(name="언어목록", description="시이가 번역할 수 있는 언어 목록을 봐요.")
async def language_list(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    language_info = "\n".join([f"- {name} (`{code}`)" for code, name in supported_languages.items()])
    embed = discord.Embed(title="🌐 시이가 할 수 있는 언어들이에요!", description=language_info, color=discord.Color.teal())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="알림", description="시이가 잊지 않도록 약속을 알려줘요!")
@app_commands.describe(내용="기억할 내용을 적어주세요!")
async def set_reminder(interaction: discord.Interaction, 내용: str):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    if await check_rate_limit(interaction): return
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    await interaction.response.send_message("📌 이 약속을 얼마나 자주 알려드릴까요?", view=ReminderFrequencyView(내용), ephemeral=True)

@bot.tree.command(name="알림목록", description="시이가 기억하고 있는 약속 목록을 봐요.")
async def list_reminders(interaction: discord.Interaction):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    reminders = guild_settings.get("reminders", [])
    if not reminders:
        await interaction.response.send_message("📌 지금은 기억하고 있는 약속이 없는걸요!", ephemeral=True)
        return
    reminder_list = [f"🔔 **{idx}.** `{r['time']}` | {r['frequency']} | 💬 {r['message']}" for idx, r in enumerate(reminders, 1)]
    await interaction.response.send_message(f"📌 **시이가 기억하고 있는 약속 목록이에요!**\n\n" + "\n\n".join(reminder_list), ephemeral=True)

@bot.tree.command(name="알림삭제", description="기억하고 있는 약속을 취소해요.")
@app_commands.describe(번호="취소할 약속의 번호를 알려주세요! (`/알림목록`으로 확인)")
async def remove_reminder(interaction: discord.Interaction, 번호: int):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    reminders = guild_settings.get("reminders", [])
    if not 1 <= 번호 <= len(reminders):
        await interaction.response.send_message("⚠ 앗, 그런 번호의 약속은 없는 것 같아요!", ephemeral=True)
        return
    removed = reminders.pop(번호 - 1)
    guild_settings["reminders"] = reminders
    save_settings(str(interaction.guild.id), guild_settings)
    await interaction.response.send_message(f"✅ 알겠어요! `{removed['message']}` 약속은 잊어버릴게요!")

@bot.tree.command(name="번역", description="[수동 번역] 원하는 텍스트를 지정한 언어로 번역해요!")
@app_commands.describe(언어="번역할 언어의 코드 (예: en, ja)", 텍스트="번역할 내용")
async def manual_translate(interaction: discord.Interaction, 언어: str, 텍스트: str):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    await interaction.response.defer(thinking=True)

    language_code = 언어.lower()
    if language_code not in supported_languages:
        await interaction.followup.send("⚠ 앗, 시이가 아직 모르는 언어 코드예요! `/언어목록`으로 확인할 수 있답니다!", ephemeral=True)
        return

    try:
        @async_retry_with_backoff()
        async def get_translation():
            target_lang_name = supported_languages.get(language_code, language_code)
            prompt = f"Translate the following text into {target_lang_name}. Just provide the translated text directly.\n\nText to translate:\n---\n{텍스트}\n---"
            response = await translation_model.generate_content_async(prompt)
            translated_text = response.text.strip()
            if not translated_text: raise ValueError("Translated text is empty.")
            return translated_text, target_lang_name

        translated_text, target_lang_name = await get_translation()
        log.info(f"-> 수동 번역 결과 ({interaction.user}): '{텍스트}' → '{translated_text}'")

        embed = discord.Embed(title="📝 시이의 수동 번역 결과랍니다!", color=discord.Color.green())
        embed.set_author(name=f"{interaction.user.display_name} 함장님의 요청", icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="원본 텍스트", value=f"```{텍스트}```", inline=False)
        embed.add_field(name=f"번역 결과 ({target_lang_name})", value=f"```{translated_text}```", inline=False)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        log.error(f"[/{interaction.command.name}] 수동 번역 중 오류: {e}")
        await interaction.followup.send("으음... 번역에 실패한 것 같아요! 잠시 후 다시 시도해 주실래요?", ephemeral=True)

@bot.tree.command(name="핑", description="시이의 응답 속도를 측정해요!")
async def ping(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"시이의 현재 반응 속도는 {latency}ms 랍니다! 🚀")

@bot.tree.command(name="도움말", description="시이가 할 수 있는 모든 것을 알려줘요!")
async def help_command(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    embed = discord.Embed(title="✨ AI 비서 시이 명령어 도움말 ✨", description="함장님을 위해 시이가 할 수 있는 일들이에요!", color=discord.Color.gold())
    embed.add_field(name="💬 대화 & 정보", value="`/시이야`, `/새대화`, `/핑`, `/확인`", inline=False)
    embed.add_field(name="🌐 번역", value="`/번역`, `/언어목록`, `/번역채널추가`, `/번역채널제거`, `/언어설정`", inline=False)
    embed.add_field(name="⏰ 알림", value="`/알림`, `/알림목록`, `/알림삭제`", inline=False)
    embed.add_field(name="👑 관리자 전용", value="`/기본채널설정`, `/설정확인`, `/설정초기화`, `/공지`", inline=False)
    embed.set_footer(text="시이는 언제나 함장님 곁에 있답니다!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="서버정보", description="현재 서버의 이름과 고유 ID를 확인해요!")
async def server_info(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    if not interaction.guild:
        await interaction.response.send_message("이 명령어는 서버 안에서만 사용할 수 있어요!", ephemeral=True)
        return
    server_name = interaction.guild.name
    server_id = interaction.guild.id
    embed = discord.Embed(title="📜 서버 정보예요!", color=discord.Color.blue())
    embed.add_field(name="🏢 서버 이름", value=server_name, inline=False)
    embed.add_field(name="🔑 고유 ID", value=str(server_id), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 서버 관리자 전용 명령어 ---
@bot.tree.command(name="기본채널설정", description="[관리자] 봇의 공지, 번역 결과 등 주요 메시지를 받을 채널을 설정해요.")
@app_commands.default_permissions(administrator=True)
async def set_main_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    guild_settings["translation_channel"] = 채널.id
    save_settings(str(interaction.guild.id), guild_settings)
    await interaction.response.send_message(f"✅ 알겠습니다! 앞으로 봇의 주요 알림은 `{채널.name}` 채널에 보내드릴게요! 💌")

@bot.tree.command(name="설정확인", description="[관리자] 현재 시이에게 설정된 서버의 채널들을 확인해요.")
@app_commands.default_permissions(administrator=True)
async def check_settings(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    source_channels_ids = guild_settings.get("source_channels", [])
    main_channel_id = guild_settings.get("translation_channel")
    source_channels_text = "\n".join([f"<#{channel_id}>" for channel_id in source_channels_ids]) if source_channels_ids else "없답니다!"
    main_channel_text = f"<#{main_channel_id}>" if main_channel_id else "아직 지정되지 않았어요!"
    embed = discord.Embed(title="📜 시이의 서버 설정 현황이에요!", color=discord.Color.blue())
    embed.add_field(name="📢 봇 기본 채널 (공지, 번역 결과 등)", value=main_channel_text, inline=False)
    embed.add_field(name="👀 자동 번역 감지 채널", value=source_channels_text, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="번역채널추가", description="[관리자] 자동 번역을 수행할 채널 목록에 추가해요.")
@app_commands.default_permissions(administrator=True)
async def add_source_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    if 채널.id not in guild_settings.get("source_channels", []):
        guild_settings.setdefault("source_channels", []).append(채널.id)
        save_settings(str(interaction.guild.id), guild_settings)
        await interaction.response.send_message(f"알겠습니다! 이제 `{채널.name}` 채널의 이야기도 번역해서 알려드릴게요! ✨")
    else:
        await interaction.response.send_message(f"`{채널.name}` 채널은 이미 지켜보고 있답니다! 👀")

@bot.tree.command(name="번역채널제거", description="[관리자] 자동 번역 채널 목록에서 제거해요.")
@app_commands.default_permissions(administrator=True)
async def remove_source_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    if 채널.id in guild_settings.get("source_channels", []):
        guild_settings["source_channels"].remove(채널.id)
        save_settings(str(interaction.guild.id), guild_settings)
        await interaction.response.send_message(f"알겠습니다! `{채널.name}` 채널의 번역 임무를 중단할게요!")
    else:
        await interaction.response.send_message(f"`{채널.name}` 채널은 원래부터 번역 목록에 없었어요!")

@bot.tree.command(name="언어설정", description="[관리자] 자동 번역될 언어를 변경해요.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(언어="번역할 언어의 코드 (예: en, ja)")
async def set_language(interaction: discord.Interaction, 언어: str):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    language_code = 언어.lower()
    if language_code not in supported_languages:
        await interaction.response.send_message("앗! 그건 시이가 아직 모르는 언어예요! `/언어목록`으로 확인할 수 있답니다!", ephemeral=True)
        return
    guild_settings["target_language"] = language_code
    save_settings(str(interaction.guild.id), guild_settings)
    await interaction.response.send_message(f"✅ 자동 번역 언어를 **{supported_languages[language_code]}**으(로) 변경했어요!")

@bot.tree.command(name="공지", description="[관리자] 시이가 지정된 채널에 메시지를 보내요!")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(채널="메시지를 보낼 채널", 메시지="보낼 내용")
async def broadcast(interaction: discord.Interaction, 채널: discord.TextChannel, 메시지: str):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    try:
        await 채널.send(메시지)
        await interaction.response.send_message(f"✅ `#{채널.name}` 채널에 공지를 성공적으로 보냈어요!", ephemeral=True)
    except Exception as e:
        log.error(f"[/공지] 메시지 전송 중 오류: {e}")
        await interaction.response.send_message(f"⚠ 메시지를 보내는 중에 오류가 발생했어요! 권한을 확인해주세요!", ephemeral=True)

@bot.tree.command(name="설정초기화", description="[관리자] 이 서버의 모든 번역 채널 설정을 초기화해요.")
@app_commands.default_permissions(administrator=True)
async def reset_channels(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    guild_settings["source_channels"] = []
    save_settings(str(interaction.guild.id), guild_settings)
    await interaction.response.send_message("✅ 알겠습니다! 자동 번역 감지 채널 설정을 깨끗하게 초기화했어요!")

# --- 봇 주인 전용 명령어 ---
@bot.tree.command(name="전체공지", description="[봇 주인] 봇이 접속한 모든 활성 서버에 공지를 보내요!", guild=OWNER_GUILD)
@app_commands.describe(메시지="모든 서버에 보낼 내용")
@app_commands.check(is_bot_owner)
async def broadcast_all(interaction: discord.Interaction, 메시지: str):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    await interaction.response.defer(ephemeral=True, thinking=True)
    success_count, fail_count = 0, 0
    for guild in bot.guilds:
        try:
            guild_settings = load_settings(str(guild.id))
            output_channel_id = guild_settings.get("translation_channel")
            if output_channel_id:
                channel = bot.get_channel(output_channel_id)
                if channel:
                    await channel.send(f"📢 **시이의 전체 공지사항이에요!**\n\n{메시지}")
                    success_count += 1
                else:
                    fail_count += 1
            else:
                fail_count += 1
        except Exception as e:
            log.error(f"  -> '{guild.name}' 서버 공지 처리 중 오류: {e}")
            fail_count += 1
    await interaction.followup.send(f"✅ 전체 공지 발송 완료! (성공: {success_count}, 실패/제외: {fail_count})", ephemeral=True)

@bot.tree.command(name="로그채널설정", description="[봇 주인] 터미널 로그를 실시간으로 받을 채널을 설정하거나 해제해요.", guild=OWNER_GUILD)
@app_commands.describe(채널="로그를 수신할 비공개 채널 (해제하려면 비워두세요)")
@app_commands.check(is_bot_owner)
async def set_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel = None):
    record_server_usage(interaction)
    if 채널:
        discord_log_handler.set_channel(채널.id)
        log.info(f"[로그 설정] 실시간 로그 전송 채널 설정 -> #{채널.name}")
        await interaction.response.send_message(f"✅ 알겠습니다! 이제 모든 터미널 로그를 `#{채널.name}` 채널로 보내드릴게요!", ephemeral=True)
    else:
        log.info("[로그 설정] 실시간 로그 전송 채널 해제")
        discord_log_handler.set_channel(None)
        await interaction.response.send_message("✅ 실시간 로그 전송을 중단했어요.", ephemeral=True)

@bot.tree.command(name="차단", description="[봇 주인] 특정 서버나 채널을 차단 목록에 추가해요.", guild=OWNER_GUILD)
@app_commands.describe(아이디="차단할 서버 또는 채널의 ID를 입력해주세요!")
@app_commands.check(is_bot_owner)
async def block_target(interaction: discord.Interaction, 아이디: str):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    try:
        target_id = int(아이디)
        blacklist = load_blacklist()
        if target_id in blacklist["blocked_servers"] or target_id in blacklist["blocked_channels"]:
            await interaction.response.send_message(f"이미 차단 목록에 있는 ID예요! ({target_id})", ephemeral=True)
            return
        if bot.get_guild(target_id):
            blacklist["blocked_servers"].append(target_id)
            save_blacklist(blacklist)
            await interaction.response.send_message(f"✅ 서버를 성공적으로 차단했어요! (ID: {target_id})", ephemeral=True)
        elif bot.get_channel(target_id):
            blacklist["blocked_channels"].append(target_id)
            save_blacklist(blacklist)
            await interaction.response.send_message(f"✅ 채널을 성공적으로 차단했어요! (ID: {target_id})", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠ 유효하지 않은 ID 같아요! 서버나 채널 ID가 맞는지 확인해주세요!", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("⚠ ID는 숫자만 입력해야 한답니다!", ephemeral=True)

@bot.tree.command(name="차단해제", description="[봇 주인] 차단된 서버나 채널을 목록에서 제거해요.", guild=OWNER_GUILD)
@app_commands.describe(아이디="차단 해제할 서버 또는 채널의 ID를 입력해주세요!")
@app_commands.check(is_bot_owner)
async def unblock_target(interaction: discord.Interaction, 아이디: str):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    try:
        target_id = int(아이디)
        blacklist = load_blacklist()
        if target_id in blacklist["blocked_servers"]:
            blacklist["blocked_servers"].remove(target_id)
            save_blacklist(blacklist)
            await interaction.response.send_message(f"✅ 서버 차단을 해제했어요! (ID: {target_id})", ephemeral=True)
        elif target_id in blacklist["blocked_channels"]:
            blacklist["blocked_channels"].remove(target_id)
            save_blacklist(blacklist)
            await interaction.response.send_message(f"✅ 채널 차단을 해제했어요! (ID: {target_id})", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠ 차단 목록에 없는 ID예요!", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("⚠ ID는 숫자만 입력해야 한답니다!", ephemeral=True)

@bot.tree.command(name="전체서버목록", description="[봇 주인] 봇이 접속한 모든 서버의 목록과 ID를 확인해요.", guild=OWNER_GUILD)
@app_commands.check(is_bot_owner)
async def list_all_servers(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    server_list = [f"🏢 **{guild.name}**\n   - ID: `{guild.id}`" for guild in bot.guilds]
    if not server_list:
        await interaction.response.send_message("아무 서버에도 접속해있지 않아요!", ephemeral=True)
        return
    description = "\n\n".join(server_list)
    embed = discord.Embed(title=f"🛰️ 시이가 접속한 총 {len(bot.guilds)}개의 서버 목록이에요!", description=description, color=discord.Color.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="테스트과부하", description="[봇 주인] 과부하 감지 시스템을 테스트합니다.", guild=OWNER_GUILD)
@app_commands.describe(횟수="가상 요청 횟수", 간격="요청 간 간격(초)")
@app_commands.check(is_bot_owner)
async def spam_test(interaction: discord.Interaction, 횟수: int = 15, 간격: float = 0.1):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name}, 사용자: {interaction.user})")
    await interaction.response.send_message(f"`{횟수}`회 가상 요청 테스트를 시작합니다...", ephemeral=True)
    for i in range(횟수):
        if await check_rate_limit(interaction):
            return
        await asyncio.sleep(간격)
    await interaction.followup.send("테스트가 끝났지만 과부하가 감지되지 않았어요.", ephemeral=True)

# 봇 주인 전용 명령어 에러 핸들러
@broadcast_all.error
@set_log_channel.error
@block_target.error
@unblock_target.error
@list_all_servers.error
@spam_test.error
async def owner_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❗ 이 명령어는 시이의 주인님만 사용할 수 있답니다!", ephemeral=True)

# ==============================
# 메시지 이벤트 핸들러
# ==============================
@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    blacklist = load_blacklist()
    if message.guild.id in blacklist["blocked_servers"]: return
    if message.channel.id in blacklist["blocked_channels"]: return

    guild_settings = load_settings(str(message.guild.id))
    if message.channel.id not in guild_settings.get("source_channels", []): return
    if not guild_settings.get("translation_channel"): return

    original_content = message.content
    if not original_content or URL_PATTERN.fullmatch(original_content.strip()): return
    if re.search(r'[\uac00-\ud7a3]', original_content): return

    content_for_filtering = re.sub(r'<a?:\w+:\d+>', '', original_content).strip() # 이모지 제거
    if not content_for_filtering or not re.search(r'[a-zA-Z\u3040-\u30ff\u4e00-\u9fff]', content_for_filtering):
        return

    log.info(f"-> 자동 번역 감지 (서버: {message.guild.name}): '{original_content}'")
    translation_channel = bot.get_channel(guild_settings["translation_channel"])
    if not translation_channel: return

    target_language = guild_settings.get("target_language", "ko")
    translated_text, detected_language = await translate_text_gemini(original_content, target_language)
    if detected_language == "error": return

    if translated_text and translated_text.strip():
        async with translation_channel.typing():
            await asyncio.sleep(0.5)
            embed = discord.Embed(color=discord.Color.og_blurple())
            embed.set_author(name=f"{message.author.display_name} 님의 메시지", icon_url=message.author.display_avatar.url, url=message.jump_url)
            embed.add_field(name="원본 메시지", value=f"```{original_content}```", inline=False)
            embed.add_field(name="번역 결과", value=f"```{translated_text}```", inline=False)
            target_lang_name = supported_languages.get(target_language, target_language)
            embed.set_footer(text=f"자동 번역: {detected_language} → {target_lang_name}")
            await translation_channel.send(embed=embed)

# 🔹 봇 실행
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)