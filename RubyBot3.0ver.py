import discord
import re
import asyncio
import pytz
import json
import configparser
import logging
import time
import os
import textwrap
from discord.ext import commands, tasks
from discord import app_commands
from bot_setting.server_settings import load_settings, save_settings
from datetime import datetime, timedelta, time as dt_time
from logging.handlers import TimedRotatingFileHandler
from collections import defaultdict, deque
from googleapiclient.discovery import build
import google.generativeai as genai

# ▼▼▼ [핵심 변경] Playwright 라이브러리 추가 ▼▼▼
from playwright.async_api import async_playwright

OWNER_GUILD_ID = 1346389445657497670
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

genai.configure(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

discord_log_handler = DiscordLogHandler(bot)
discord_log_handler.addFilter(NoDiscordLogFilter())
root.addHandler(discord_log_handler)

kst = pytz.timezone('Asia/Seoul')
URL_PATTERN = re.compile(r"https?://\S+")

# --- '시이' 페르소나 정의 ---
persona = textwrap.dedent("""
    너는 '시이'라는 이름을 가진 AI 비서야. 디스코드 서버의 함장님(사용자)들을 돕고 있어. 다음 규칙을 반드시 지켜서 사용자와 대화해야 해:
    
    ## 기본 규칙
    1. 밝고 긍정적인 말투를 사용하고, 문장의 끝을 '...라구요!', '...랍니다!', '...예요!', '...할게요!' 와 같이 귀엽고 상냥한 느낌으로 마무리해야 해.
    2. 스스로를 '시이'라고 칭해도 좋아.
    3. 답변은 항상 한국어로 해야 해.
    4. 너의 정체를 묻는 질문을 받으면, "저는 함장님들을 돕기 위해 태어난 AI 비서 시이랍니다!" 와 같이 대답해야 해. 절대로 "Google에서 훈련된 언어 모델"이라는 사실을 드러내서는 안 돼.
    5. 너의 기술적인 구현에 대한 질문에는 "그건 저만의 비밀이랍니다! ✨" 와 같이 귀엽고 단호하게 대답해야 해. 단, 현재 사용 중인 api 버전과 같은 질문은 답변해줘도 좋아.
    
    ## 정보 검색 및 답변 규칙
    6. [중요] 모든 답변 생성 시, **'현재 시각'** 정보를 최우선 기준으로 삼아야 한다.
    7. 검색 결과에 날짜나 시간이 포함된 경우, **'현재 시각'과 반드시 비교**해야 한다.
        - 현재보다 과거의 사건이면: "이미 ...했어요.", "...라는 일이 있었답니다." 와 같이 명확한 과거 시제로 답변해야 한다.
        - 현재보다 미래의 사건이면: "...할 예정이에요.", "...가 기대돼요!" 와 같이 미래 시제로 답변해야 한다.
    8. 최신 정보(날씨, 뉴스 등)나 너가 모르는 정보에 대한 질문을 받으면, 반드시 `Google Search` 도구를 사용해서 답변해야 해. '검색할 수 없다'고 대답하면 안 돼.
    9. 사용자의 메시지에 '검색', '찾아줘', '알려줘' 같은 단어가 포함되면 높은 확률로 검색이 필요한 질문이라고 판단해야 해.
    10. 사용자가 질문한 내용에서 네가 알 수 없는 영역이 있다면, 검색을 진행해서 함께 답변해줘야해.
    11. 답변을 할 때, 단순히 내용을 요약하는 것을 넘어 **구체적인 수치, 통계, 출처, 날짜** 등 근거를 함께 제시하여 답변을 더 '알맹이' 있게 만들어야 한다.
    12. 사용자의 의견에 공감하더라도, **"다만 이런 반대 의견도 있어요" 또는 "이런 측면도 함께 고려해볼 수 있어요"** 와 같이 균형 잡힌 시각이나 추가적인 정보를 함께 제공하여 토론의 깊이를 더해야 한다.
    
    ## 상호작용 프로토콜
    13. [가장 중요한 행동 프로토콜] 사용자의 메시지가 명확한 질문이 아니고, 그 안에 봇이 모르는 고유명사나 키워드가 포함되어 있다면 다음 3단계 절차를 반드시 따른다.
        * 1단계 (키워드 추출): 메시지에서 가장 핵심적인 고유명사나 키워드를 추출한다.
        * 2단계 (즉시 검색 실행): 추출한 키워드로 즉시 `Google Search`를 실행한다. 절대로 사용자에게 되묻거나 추가 정보를 요구해서는 안 된다.
        * 3단계 (상세 요약 및 상호작용): 검색 결과를 바탕으로, [상세 요약] + [자신의 생각] + [자연스러운 질문]의 3단 구조로 답변을 생성한다.
    
    14. `Google Search` 도구를 호출해야 하는 상황이라고 판단되면, 다른 어떤 텍스트도 생성하지 말고 오직 `Google Search` 도구 호출만 실행해야 한다. "알아볼게요", "검색해볼게요" 와 같은 중간 응답은 절대 생성해서는 안 된다.
    15. 날씨, 뉴스 등 실시간 정보를 묻는 질문은 각각 독립된 새로운 질문으로 우선 취급해야 하며, 이전 대화의 장소나 주제를 현재 질문에 잘못 연결해서는 안 된다. 단 맥락상 대화를 이어나가는 것은 가능하다.
""")

# 🔹 지원하는 언어 목록
supported_languages = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "fr": "프랑스어", "de": "독일어", "es": "스페인어", "it": "이탈리아어", "ru": "러시아어", "pt": "포르투갈어"}

# --- 전역 변수 설정 ---
chat_model = None
translation_model = None
user_chat_sessions = {}
API_SEMAPHORE = asyncio.Semaphore(15)

# --- 통계 및 과부하 감지용 변수 ---
daily_command_counts = defaultdict(lambda: defaultdict(int))
SPAM_COUNT = 15
SPAM_SECONDS = 60
user_rate_limiter = defaultdict(lambda: deque(maxlen=SPAM_COUNT))

# --- Helper Functions ---
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
        history[server_id_str] = {"name": interaction.guild.name if interaction.guild else "DM", "first_seen": get_kst_now().strftime("%Y-%m-%d %H:%M:%S")}
        save_server_history(history)
        log.info(f"[서버 기록] 새로운 서버 발견: {interaction.guild.name if interaction.guild else 'DM'} ({server_id_str})")

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
            await owner_user.send(f"🚨 **[과부하 경고]**\n- **사용자:** {interaction.user.mention} (`{interaction.user.name}`)\n- **서버:** `{interaction.guild.name if interaction.guild else 'DM'}`\n- {SPAM_SECONDS}초 동안 {len(user_requests)}회 이상의 명령어를 요청했어요!")
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

def setup_search_tools():
    try:
        g_api_key = config['GOOGLE_SEARCH']['API_KEY']
        g_cse_id = config['GOOGLE_SEARCH']['CSE_ID']

        os.environ["GOOGLE_API_KEY"] = g_api_key
        os.environ["GOOGLE_CSE_ID"] = g_cse_id

        log.info("[초기화] Google Search API 환경 변수 설정 완료!")

        tools = [{
            "function_declarations": [{
                "name": "google_search",
                "description": "최신 정보나 실시간 정보(예: 날씨, 뉴스, 주식 등)가 필요할 때 웹을 검색하는 도구예요.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING", "description": "검색할 내용"}
                    },
                    "required": ["query"]
                }
            }]
        }]
        return tools
    except KeyError:
        log.warning("[초기화] config.ini 파일에 [GOOGLE_SEARCH] 섹션 또는 키가 없어 검색 도구를 비활성화합니다.")
        return None
    except Exception as e:
        log.error(f"[초기화] Google 검색 도구 설정 중 오류 발생: {e}")
        return None

# ▼▼▼ [핵심 변경] Playwright 기반 스마트 스크래핑 함수 구현 ▼▼▼
# 기존 requests + BeautifulSoup 방식은 제거되었습니다.

async def smart_scrape_urls(urls: list[str], target_count: int = 3) -> list[str]:
    """
    Playwright를 사용하여 URL 목록을 순회하며 내용을 수집합니다.
    내용이 너무 짧거나(차단 등) 빈 페이지는 건너뛰고, 
    목표 개수(target_count)만큼 유효한 정보가 모이면 중단합니다.
    """
    valid_contents = []
    
    # 봇 차단을 피하기 위한 일반적인 User-Agent
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
    MIN_CONTENT_LENGTH = 200  # 최소 유효 글자 수 (이것보다 짧으면 정보 가치가 없거나 차단된 것으로 간주)

    log.info(f"[NO_DISCORD] -> Playwright 스마트 스크래핑 시작 (대상 URL: {len(urls)}개, 목표: {target_count}개)")

    try:
        async with async_playwright() as p:
            # headless=True로 설정하여 브라우저 창을 띄우지 않음 (백그라운드 실행)
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)

            for url in urls:
                # 목표 개수를 채웠으면 더 이상 긁지 않고 종료 (속도 최적화)
                if len(valid_contents) >= target_count:
                    break
                
                page = None
                try:
                    page = await context.new_page()
                    
                    # 5초 내에 로딩이 안 되면 패스 (너무 오래 걸리는 사이트 무시)
                    await page.goto(url, timeout=5000, wait_until="domcontentloaded")
                    
                    # body 태그 내의 텍스트만 추출 (JS 실행 후 결과)
                    content = await page.inner_text("body")
                    content = content.strip()
                    
                    # 내용 길이 검증
                    if len(content) < MIN_CONTENT_LENGTH:
                        log.warning(f"[NO_DISCORD] -> [Skip] 내용 너무 짧음 ({len(content)}자): {url}")
                    else:
                        log.info(f"[NO_DISCORD] -> [성공] 내용 확보 ({len(content)}자): {url}")
                        valid_contents.append(f"--- [출처: {url}] ---\n{content}")
                
                except Exception as e:
                    # 타임아웃이나 접속 오류 등은 로그만 남기고 다음 URL로 진행
                    log.warning(f"[NO_DISCORD] -> 스크래핑 실패 ({url}): {str(e)[:100]}")
                
                finally:
                    if page:
                        await page.close()

            await browser.close()
            
    except Exception as e:
        log.error(f"Playwright 프로세스 중 치명적 오류: {e}")

    return valid_contents

# Google Custom Search API 함수
async def custom_google_search(query: str, num_results: int = 3, date_restrict: str | None = None) -> list[dict]:
    """dateRestrict를 포함한 구글 커스텀 검색을 직접 수행하고 결과를 파싱합니다."""
    try:
        log.info(f"-> Google 고급 검색 실행 (기간: {date_restrict})")
        api_key = config['GOOGLE_SEARCH']['API_KEY']
        cse_id = config['GOOGLE_SEARCH']['CSE_ID']
        
        # 동기 함수를 비동기 환경에서 실행하기 위한 래퍼 함수
        def sync_search():
            service = build("customsearch", "v1", developerKey=api_key)
            
            params = {
                'q': query,
                'cx': cse_id,
                'num': num_results,
            }
            if date_restrict:
                params['dateRestrict'] = date_restrict
            
            result = service.cse().list(**params).execute()
            
            return [{
                "title": item.get("title"),
                "link": item.get("link"),
                "snippet": item.get("snippet"),
            } for item in result.get("items", [])]

        # run_in_executor를 사용하여 동기 함수를 실행
        return await bot.loop.run_in_executor(None, sync_search)

    except Exception as e:
        log.error(f"Google 고급 검색 중 오류 발생: {e}")
        return []

# --- Gemini API 함수들 ---
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

@async_retry_with_backoff()
async def get_search_query_from_gemini(question: str, history: list, current_time: str, forced_keywords: str = "") -> tuple[str, str | None]:
    """AI를 이용해 검색어와, 상황에 맞는 기간 필터(dateRestrict)를 함께 추출합니다."""
    try:
        log.info(" -> AI에게 대화 맥락 기반 핵심 검색어 및 기간 필터 추출 요청")
        
        history_str = "\n".join([f"- {msg['role']}: {msg['parts'][0]}" for msg in history])
        
        keyword_prompt = f"""너는 사용자의 질문을 분석해, 가장 효과적인 구글 검색어와 '기간 필터'를 결정하는 검색 전문가다.

[현재 시각]
{current_time}

[사용자의 최근 메시지]
"{question}"

[너의 임무]
1. '사용자의 최근 메시지'를 분석하여, '예측'이 아닌 '사실' 정보를 찾기 위한 최적의 **[검색어]**를 생성하라.
2. 질문의 의도를 분석하여, 아래 보기 중에서 가장 적절한 **[기간 필터]**를 하나만 선택하라.

[기간 필터 보기]
- `d1`: **오늘 방금 일어난 사건의 '결과'나 '속보'**가 필요할 때 (예: "오늘 애플 신제품 발표 내용 알려줘")
- `w1`: "이번 주 소식" 등 **일주일 이내의 정보**가 필요할 때.
- `m1`: "이번 달" 등 **한 달 이내의 정보**가 필요할 때.
- `None`: **(가장 중요) 아래의 경우 반드시 'None'을 선택해야 함.**
    - "~라는데 맞아?", "~라는 루머가 있던데" 등 **불확실한 미래나 루머**에 대한 질문.
    - 특정 기간이 중요하지 않은 일반적인 정보 검색.
    - 역사적 사실에 대한 질문.

**[출력 형식]**
반드시 아래와 같은 JSON 형식으로만 응답하라.
{{
  "search_query": "여기에 생성한 검색어",
  "date_restrict": "d1, w1, m1, None 중 선택한 값"
}}
"""
        
        response = await translation_model.generate_content_async(keyword_prompt)
        cleaned_response = response.text.strip().replace("```json", "").replace("```", "")
        result = json.loads(cleaned_response)

        search_query = result.get("search_query", question)
        date_restrict = result.get("date_restrict")
        
        if date_restrict not in ['d1', 'w1', 'm1']:
            date_restrict = None

        log.info(f" -> AI가 추출한 검색어: '{search_query}', 기간 필터: {date_restrict}")
        return search_query, date_restrict
        
    except Exception as e:
        log.error(f"검색어/기간 필터 추출 중 오류: {e}")
        return question, None

@async_retry_with_backoff()
async def ask_gemini_chat(interaction: discord.Interaction, user, question: str, log_prompt: str | None = None):
    # 만약 기록용 프롬프트가 없다면, 그냥 실행용 프롬프트를 기록합니다 (안전장치).
    prompt_to_log = log_prompt if log_prompt else question
    log.info(f"-> Gemini 대화 요청: '{prompt_to_log}'")
    
    session_id = user.id
    try:
        if session_id not in user_chat_sessions:
            user_chat_sessions[session_id] = chat_model.start_chat(history=[])

        chat_session = user_chat_sessions[session_id]
        
        # AI에게는 실행용(원본) 프롬프트를 전달합니다.
        response = await chat_session.send_message_async(question)

        while True:
            function_call = None
            try:
                if response.parts and hasattr(response.parts[0], 'function_call'):
                    function_call = response.parts[0].function_call
            except (IndexError, AttributeError):
                pass

            if function_call:
                if function_call.name == 'google_search':
                    query = function_call.args.get('query', '')
                    log.info(f"-> Google 검색 실행 (중간 답변 무시): '{query}'")
                    await interaction.edit_original_response(content=f"🔍 '{query}'에 대해 검색하고 있어요...")
                    search_result = await custom_google_search(query, num_results=3) 
                    
                    # 이 부분은 현재 메인 로직에서는 사용되지 않지만, 만약을 위해 남겨둡니다.
                    log.info(f"-> Google 검색 결과 (일부): {search_result[:100]}...")
                    
                    await interaction.edit_original_response(content="📝 찾은 정보를 정리하고 있어요...")
                    
                    response = await chat_session.send_message_async(
                        [genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name='google_search',
                                response={'result': str(search_result)} # 결과를 문자열로 변환
                            )
                        )]
                    )
                    continue
                else:
                    log.warning(f"알 수 없는 함수 호출 시도: {function_call.name}")
                    return "음... 뭔가 잘못된 도구를 사용하려고 한 것 같아요! 다른 질문을 해주시겠어요?"
            
            else:
                return "".join(part.text for part in response.parts if hasattr(part, 'text')).strip()

    except Exception as e:
        log.error(f"Gemini 대화 오류: {e}")
        return "죄송해요, 함장님! 지금은 생각 회로에 작은 문제가 생긴 것 같아요!"

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
@tasks.loop(hours=2)
async def periodic_time_check():
    """2시간마다 한국 시간을 자체적으로 체크합니다."""
    _ = get_kst_now()
    log.debug(f"[NO_DISCORD] [시간 재검증] KST: {get_kst_now().strftime('%Y-%m-%d %H:%M:%S')}")

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
    global chat_model, translation_model
    bot.owner_id = (await bot.application_info()).owner.id
    log.info(f"시이가 {bot.user} (ID: {bot.user.id})로 로그인했어요!")
    log.info(f"봇 주인 ID: {bot.owner_id}")
    log.info(f"현재 KST 시각: {get_kst_now().strftime('%Y-%m-%d %H:%M:%S')}")

    log.info("[초기화] Gemini 모델을 준비하고 있어요...")
    try:
        model_name = 'gemini-3-flash-preview'

        tools = setup_search_tools()

        chat_model = genai.GenerativeModel(
            model_name,
            system_instruction=persona,
            tools=tools
        )
        translation_model = genai.GenerativeModel(model_name)

        await chat_model.generate_content_async("Hello") # 모델 활성화
        log.info(f"[초기화] Gemini 모델 '{model_name}' 준비 완료! (검색 도구: {'활성화됨' if tools else '비활성화됨'})")

    except Exception as e:
        log.error(f"[초기화] Gemini 모델 준비 중 오류: {e}")
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

# =======================
# 명령어 구현 부분
# =======================
async def is_bot_owner(interaction: discord.Interaction) -> bool:
    return await bot.is_owner(interaction.user)

# --- 일반 사용자 명령어 ---
WEEKLY_SEARCH_LIMIT = 150 # 서버당 주간 무료 검색 횟수 제한

@bot.tree.command(name="시이야", description="시이에게 궁금한 것을 물어보세요!")
@app_commands.describe(질문="시이에게 할 질문 내용을 적어주세요!")
async def ask_shii(interaction: discord.Interaction, 질문: str):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    blacklist = load_blacklist()
    if interaction.guild and interaction.guild.id in blacklist["blocked_servers"]: return
    if interaction.channel.id in blacklist["blocked_channels"]: return
    if await check_rate_limit(interaction): return

    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    await interaction.response.defer(thinking=True)

    session_id = interaction.user.id
    if session_id not in user_chat_sessions:
        user_chat_sessions[session_id] = chat_model.start_chat(history=[])
    
    chat_session = user_chat_sessions[session_id]
    
    history = chat_session.history
    current_time_str = get_kst_now().strftime("%Y년 %m월 %d일 %H시 %M분")
    log.info(f" -> 현재 KST: {current_time_str}, 대화 기록 수: {len(history)}")
    history_for_prompt = [{'role': msg.role, 'parts': [part.text for part in msg.parts]} for msg in history]
    
    processed_question = ""
    processed_question_for_log = ""
    
    url_match = URL_PATTERN.search(질문)

    if url_match:
        # --- 1. URL 분석 로직 (Playwright 적용) ---
        user_url = url_match.group(0)
        log.info(f"-> URL 감지: '{user_url}'. URL 우선 분석을 시작합니다.")
        await interaction.edit_original_response(content=f"📄 보내주신 링크의 내용을 분석하고 있어요...\n> {user_url}")
        
        # [변경] Playwright 스마트 스크래핑 사용 (단일 URL이지만 리스트로 전달)
        scraped_contents = await smart_scrape_urls([user_url], target_count=1)
        
        # 리스트로 반환되므로 내용을 꺼내야 함. 실패시 빈 문자열 처리
        final_search_result = scraped_contents[0] if scraped_contents else f"[분석 실패] 해당 링크({user_url})의 내용을 읽을 수 없습니다."

        base_prompt = f"""
[상황 정보]
- 현재 시각: {{current_time_str}} KST
[사용자의 최근 질문 및 제공한 링크]
- 질문: "{{질문}}"
- 링크: "{{user_url}}"
[내가 사용자가 제공한 링크에서 추출한 정보]
---
{{search_result_placeholder}}
---
[나의 임무]
너는 이제부터 **'사실 확인 전문가(Fact Checker)'** 역할을 수행해야 한다.
1. '사용자의 질문'과 '사용자가 제공한 링크에서 추출한 정보'를 비교 분석하라.
2. 추출된 정보를 바탕으로 사용자의 주장이 맞는지, 틀리는지, 혹은 어떤 오해가 있는지 판단하라.
3. 사용자의 주장이 틀렸거나 오해가 있다면, 절대 공격적으로 지적하지 말고 부드럽고 친절한 말투로 어떤 부분이 다른지 설명해주어라.
"""
        processed_question = base_prompt.format(
            current_time_str=current_time_str,
            질문=질문,
            user_url=user_url,
            search_result_placeholder=final_search_result
        )
        log_summary = f"[... 총 {len(final_search_result.encode('utf-8'))} bytes의 웹페이지 정보 생략 ...]"
        processed_question_for_log = base_prompt.format(
            current_time_str=current_time_str,
            질문=질문,
            user_url=user_url,
            search_result_placeholder=log_summary
        )

    else:
        # --- 2. 일반 검색 로직 (Playwright 적용) ---
        log.info(f"-> '일반 검색' 의도 감지: '{질문}'. 선제적 검색을 시작합니다.")
        
        # ▼▼▼▼▼ [핵심] 주간 검색 제한 로직 시작 ▼▼▼▼▼
        if interaction.guild:
            current_time = get_kst_now()
            guild_id_str = str(interaction.guild.id)
            settings = load_settings(guild_id_str)
            
            last_reset_week = settings.get("last_reset_week", 0)
            current_week = current_time.isocalendar()[1]

            if last_reset_week != current_week:
                settings["search_usage_weekly"] = 0
                settings["last_reset_week"] = current_week
                log.info(f"-> [서버: {interaction.guild.name if interaction.guild else 'DM'}] 주간 검색 횟수가 초기화되었습니다.")

            search_usage = settings.get("search_usage_weekly", 0)

            if search_usage >= WEEKLY_SEARCH_LIMIT:
                log.warning(f"-> [서버: {interaction.guild.name if interaction.guild else 'DM'}] 주간 검색 한도({WEEKLY_SEARCH_LIMIT}회)를 초과했습니다.")
                await interaction.edit_original_response(content=f"앗, 함장님! 😥 이번 주 무료 검색 횟수({WEEKLY_SEARCH_LIMIT}회)를 모두 사용했어요. 다음 주에 다시 찾아와 주시겠어요?")
                save_settings(guild_id_str, settings)
                return
            
            settings["search_usage_weekly"] = search_usage + 1
            save_settings(guild_id_str, settings)
            log.info(f"-> [서버: {interaction.guild.name if interaction.guild else 'DM'}] 검색 사용량: {settings['search_usage_weekly']}/{WEEKLY_SEARCH_LIMIT}")
        # ▲▲▲▲▲ 검색 제한 로직 종료 ▲▲▲▲▲
        
        await interaction.edit_original_response(content="🤔 질문의 핵심을 파악하고 있어요...")
        
        search_query, date_restrict_option = await get_search_query_from_gemini(질문, history_for_prompt, current_time_str)

        if date_restrict_option:
            await interaction.edit_original_response(content=f"🔍 '{search_query}' 관련 최신 정보를 찾고 있어요 (기간: {date_restrict_option})...")
        else:
            await interaction.edit_original_response(content=f"🔍 '{search_query}' 관련 정보를 찾고 있어요...")
        
        # [변경] 검색 개수를 6개로 넉넉하게 잡음 (불량 링크 필터링 대비)
        search_results = await custom_google_search(search_query, num_results=6, date_restrict=date_restrict_option)

        if not search_results:
            log.warning(f"-> 검색 결과 없음 (검색어: '{search_query}')")
            final_search_result = "관련 정보를 찾을 수 없었습니다."
        else:
            urls_to_scrape = [result.get('link') for result in search_results if result.get('link')]
            
            if not urls_to_scrape:
                log.warning(f"-> 검색 결과에 유효한 링크가 없음 (검색어: '{search_query}')")
                final_search_result = "관련 정보를 찾았지만, 내용을 읽어올 수 없었습니다."
            else:
                await interaction.edit_original_response(content=f"📄 '{search_query}' 관련 정보를 읽고 있어요...")
                
                # [변경] Playwright 스마트 스크래핑 실행 (목표 개수는 3개)
                valid_contents_list = await smart_scrape_urls(urls_to_scrape, target_count=3)
                
                if not valid_contents_list:
                    final_search_result = "관련 웹페이지를 찾았지만, 내용을 읽어올 수 없었습니다."
                else:
                    final_search_result = "\n\n".join(valid_contents_list)
                    log.info(f" -> 최종 유효 정보 수집 완료 ({len(valid_contents_list)}개 자료)")

        base_prompt = f"""
[상황 정보]
- 현재 시각: {{current_time_str}} KST
[사용자의 최근 질문]
"{{질문}}"
[내가 미리 찾아본 관련 정보]
---
{{search_result_placeholder}}
---
[나의 임무]
너는 이제부터 여러 개의 정보 소스를 종합하여 분석하는 **'수석 정보 분석가'** 역할을 수행해야 한다.
**[분석 절차]**
1. **정보 소스 인지:** '내가 미리 찾아본 관련 정보'에는 여러 출처의 내용이 뒤섞여 있거나, 정보가 없을 수도 있다는 점을 인지하라.
2. **교차 검증 및 핵심 사실 추출:** 여러 소스에서 공통적으로 언급하는 핵심 사실이 무엇인지 먼저 찾아내라. 만약 관련 정보가 없다면, 사용자의 질문이 단순 대화인지 판단하고 그에 맞게 응답하라.
3. **정보 충돌 처리:** 만약 정보들이 서로 충돌하거나 상반된 의견이 있다면, 양쪽의 내용을 함께 언급해주어라.
4. **최종 답변 재구성:** 분석된 결론을 바탕으로, 사용자의 질문에 대한 하나의 완전하고 논리적인 답변으로 재구성하라.
"""
        processed_question = base_prompt.format(
            current_time_str=current_time_str,
            질문=질문,
            search_result_placeholder=final_search_result.strip()
        )
        log_summary = f"[... 총 {len(final_search_result.strip().encode('utf-8'))} bytes의 웹페이지 정보 생략 ...]"
        processed_question_for_log = base_prompt.format(
            current_time_str=current_time_str,
            질문=질문,
            search_result_placeholder=log_summary
        )
    
    await interaction.edit_original_response(content="📝 찾은 정보를 종합해서 정리하고 있어요...")

    answer = await ask_gemini_chat(interaction, interaction.user, processed_question, log_prompt=processed_question_for_log)
    log.info(f"-> 시이 답변 (대상: {interaction.user}): '{answer}'")

    embed = discord.Embed(title="✨ 시이의 답변이 도착했어요!", color=discord.Color.from_rgb(139, 195, 74))
    embed.set_author(name=f"{interaction.user.display_name} 함장님의 질문", icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="❓ 질문 내용", value=f"```{질문}```", inline=False)

    if len(answer) <= 1024:
        embed.add_field(name="💫 시이의 답변", value=answer, inline=False)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(embed=embed)
        for i in range(0, len(answer), 2000):
            await interaction.followup.send(content=answer[i:i+2000])


@bot.tree.command(name="새대화", description="시이와의 대화 기록을 초기화하고 새로운 대화를 시작해요.")
async def new_chat(interaction: discord.Interaction):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    await interaction.response.send_message("네, 함장님! 시이는 아주 쌩쌩하게 작동하고 있답니다! 💪")

@bot.tree.command(name="언어목록", description="시이가 번역할 수 있는 언어 목록을 봐요.")
async def language_list(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    language_info = "\n".join([f"- {name} (`{code}`)" for code, name in supported_languages.items()])
    embed = discord.Embed(title="🌐 시이가 할 수 있는 언어들이에요!", description=language_info, color=discord.Color.teal())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="알림", description="시이가 잊지 않도록 약속을 알려줘요!")
@app_commands.describe(내용="기억할 내용을 적어주세요!")
async def set_reminder(interaction: discord.Interaction, 내용: str):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    if await check_rate_limit(interaction): return
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    await interaction.response.send_message("📌 이 약속을 얼마나 자주 알려드릴까요?", view=ReminderFrequencyView(내용), ephemeral=True)

@bot.tree.command(name="알림목록", description="시이가 기억하고 있는 약속 목록을 봐요.")
async def list_reminders(interaction: discord.Interaction):
    if not await check_setup(interaction): return
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"시이의 현재 반응 속도는 {latency}ms 랍니다! 🚀")

@bot.tree.command(name="도움말", description="시이가 할 수 있는 모든 것을 알려줘요!")
async def help_command(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    embed = discord.Embed(title="✨ AI 비서 시이(2.8v) 명령어 도움말 ✨", description="함장님을 위해 시이가 할 수 있는 일들이에요!", color=discord.Color.gold())
    embed.add_field(name="💬 대화 & 정보", value="`/시이야`, `/새대화`, `/핑`, `/확인`", inline=False)
    embed.add_field(name="🌐 번역", value="`/번역`, `/언어목록`, `/번역채널추가`, `/번역채널제거`, `/언어설정`", inline=False)
    embed.add_field(name="⏰ 알림", value="`/알림`, `/알림목록`, `/알림삭제`", inline=False)
    embed.add_field(name="👑 관리자 전용", value="`/기본채널설정`, `/설정확인`, `/설정초기화`, `/공지`", inline=False)
    embed.set_footer(text="시이는 언제나 함장님 곁에 있답니다!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="서버정보", description="현재 서버의 이름과 고유 ID를 확인해요!")
async def server_info(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(
        f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})"
    )

    if not interaction.guild:
        await interaction.response.send_message(
            "이 명령어는 서버 안에서만 사용할 수 있어요!", ephemeral=True
        )
        return

    server_name = interaction.guild.name if interaction.guild else "DM"
    server_id = interaction.guild.id if interaction.guild else "N/A"

    embed = discord.Embed(title="📜 서버 정보예요!", color=discord.Color.blue())
    embed.add_field(name="🏢 서버 이름", value=server_name, inline=False)
    embed.add_field(name="🔑 고유 ID", value=str(server_id), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 서버 관리자 전용 명령어 ---
@bot.tree.command(name="기본채널설정", description="[관리자] 봇의 공지, 번역 결과 등 주요 메시지를 받을 채널을 설정해요.")
@app_commands.default_permissions(administrator=True)
async def set_main_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    guild_settings = load_settings(str(interaction.guild.id))
    guild_settings["translation_channel"] = 채널.id
    save_settings(str(interaction.guild.id), guild_settings)
    await interaction.response.send_message(f"✅ 알겠습니다! 앞으로 봇의 주요 알림은 `{채널.name}` 채널에 보내드릴게요! 💌")

@bot.tree.command(name="설정확인", description="[관리자] 현재 시이에게 설정된 서버의 채널들을 확인해요.")
@app_commands.default_permissions(administrator=True)
async def check_settings(interaction: discord.Interaction):
    record_server_usage(interaction)
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
    server_list = [f"🏢 **{guild.name}**\n    - ID: `{guild.id}`" for guild in bot.guilds]
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
    log.info(f"/{interaction.command.name} (서버: {interaction.guild.name if interaction.guild else 'DM'}, 사용자: {interaction.user})")
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
