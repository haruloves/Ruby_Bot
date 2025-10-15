import json
import os

# 🔹 서버별 설정 파일을 저장할 디렉터리
SETTINGS_DIR = "bot_setting/server_settings"
os.makedirs(SETTINGS_DIR, exist_ok=True)  # 폴더가 없으면 생성

def get_guild_settings_path(guild_id):
    """서버별 설정 파일 경로 반환"""
    return os.path.join(SETTINGS_DIR, f"{guild_id}.json")

def load_settings(guild_id):
    """서버별 JSON 파일에서 설정 불러오기"""
    file_path = get_guild_settings_path(guild_id)
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 기본 설정값
    return {
        "source_channels": [],
        "translation_channel": None,
        "admin_roles": [],
        "target_language": "ko",
        "reminders": [],  # 리마인더 저장용 리스트 추가
        "search_usage_weekly": 0,
        "last_reset_week": 0
    }

def save_settings(guild_id, settings):
    """서버별 JSON 파일에 설정 저장"""
    file_path = get_guild_settings_path(guild_id)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)
