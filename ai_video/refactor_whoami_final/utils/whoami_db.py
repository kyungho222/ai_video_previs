"""Chatty database and filesystem helpers, separate from shared model management."""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import traceback
from typing import List, Optional, Tuple
import yaml

from config import Config
from logs.logging_util import LoggerSingleton
from db.db_operations import execute_query
from db.db_config import connect_db, return_connection, is_transient_postgres_error
from db.db_redis import get_redis
if __package__:
    from .whoami import get_ai_config
else:
    from whoami import get_ai_config

logger = LoggerSingleton.get_logger(logger_name="chat.faiss_process", level=logging.INFO)


class ChatLookupUnavailable(RuntimeError):
    """A transient database failure prevented chatbot lookup."""


def _session_chat_id_cache_key(db_name, chat_bot_id, user_id, session_id):
    if not db_name or not chat_bot_id or not session_id:
        return None
    identity = json.dumps([db_name, chat_bot_id, "test" if user_id is None else user_id, session_id])
    return "chatbot:session_chat_id:" + hashlib.sha256(identity.encode()).hexdigest()


async def cache_session_chat_id(db_name, chat_bot_id, user_id, session_id, chat_id, *, expire_time=86400):
    """Refresh the resolved chat ID with the conversation's expiry, without blocking on Redis failure."""
    key = _session_chat_id_cache_key(db_name, chat_bot_id, user_id, session_id)
    if key is None or chat_id is None:
        return
    try:
        redis_client = await get_redis()
        if expire_time <= 0:
            await asyncio.wait_for(redis_client.delete(key), timeout=1)
        else:
            await asyncio.wait_for(
                redis_client.set(key, json.dumps(chat_id, default=str), ex=expire_time), timeout=1,
            )
    except Exception as exc:
        logger.warning("chat_id 세션 캐시 저장 실패: db=%s chatbot=%s exception=%r", db_name, chat_bot_id, exc)


async def get_chat_id_from_db(db_name, chat_bot_id, *, session_id=None, user_id=None):
    """
    주어진 db_name의 chatbot_setup 테이블에서 chat_bot_id에 해당하는 chat_id를 반환합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 조회할 챗봇의 chat_bot_id

    Returns:
        str: chat_id (존재하지 않으면 None 반환)

    Raises:
        ChatLookupUnavailable: 일시적인 DB 연결·조회 장애. 미존재와 구분한다.
    """
    key = _session_chat_id_cache_key(db_name, chat_bot_id, user_id, session_id)
    if key is not None:
        try:
            redis_client = await get_redis()
            cached = await asyncio.wait_for(redis_client.get(key), timeout=1)
            if cached:
                chat_id = json.loads(cached)
                if isinstance(chat_id, (str, int)) and not isinstance(chat_id, bool) and chat_id != "":
                    logger.info("chat_id 세션 캐시 조회 성공: db=%s chatbot=%s", db_name, chat_bot_id)
                    return chat_id
        except Exception as exc:
            logger.warning("chat_id 세션 캐시 조회 실패: db=%s chatbot=%s exception=%r", db_name, chat_bot_id, exc)
    try:
        query = """
            SELECT chat_id
            FROM chatbot_setup
            WHERE chat_bot_id = $1
        """
        result = await execute_query(
            query, params=(chat_bot_id,), fetch=True, dbname=db_name
        )
        # 로깅 전에 결과 확인
        logger.info(f"Query result: {result}")

        # None 체크 추가
        if result is None:
            return None

        if len(result) > 0:
            return result[0][0]  # chat_id 반환
        return None
    except Exception as e:
        logger.error("chat_id 조회 실패: db=%s chatbot=%s exception=%r", db_name, chat_bot_id, e)
        if is_transient_postgres_error(e):
            raise ChatLookupUnavailable("챗봇 설정 DB에 일시적으로 연결할 수 없습니다.") from e
        raise


async def get_upload_dir_from_db(db_name: str, chat_bot_id: str) -> Optional[str]:
    """
    chatbot_setup 테이블에서 upload_directory 정보를 조회합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 챗봇 ID

    Returns:
        Optional[str]: upload_directory 경로 (없으면 None)
    """
    try:
        query = """
            SELECT upload_directory
            FROM chatbot_setup
            WHERE chat_bot_id = $1
        """
        result = await execute_query(
            query, params=(chat_bot_id,), fetch=True, dbname=db_name
        )
        logger.info(f"get_dir_result:{result}")
        if result and len(result) > 0:
            return result[0][0]
        return None

    except Exception as e:
        logger.error(f"upload_directory 조회 실패: {e}")
        return None


async def get_or_create_upload_dir(db_name: str, chat_bot_id: str) -> Tuple[bool, str]:
    """
    업로드 디렉토리 정보를 조회하고, 없으면 생성합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 챗봇 ID

    Returns:
        Tuple[bool, str]: (성공 여부, 디렉토리 경로)
    """
    try:
        # 1. DB에서 디렉토리 정보 조회
        upload_dir = await get_upload_dir_from_db(db_name, chat_bot_id)
        if not upload_dir:
            logger.error("업로드 디렉토리 정보를 찾을 수 없습니다.")
            return False, ""

        # 2. 전체 경로 생성
        full_path = os.path.join(Config.UPLOAD_FOLDER, upload_dir)

        # 3. 디렉토리 확인 및 생성
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            logger.info(f"업로드 디렉토리 생성 완료: {full_path}")

        return True, full_path

    except Exception as e:
        logger.error(f"업로드 디렉토리 처리 중 오류 발생: {e}")
        return False, ""

async def get_or_create_upload_pdf_dir(db_name: str, chat_bot_id: str) -> Tuple[bool, str]:
    """
    업로드 디렉토리 정보를 조회하고, 없으면 생성합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 챗봇 ID

    Returns:
        Tuple[bool, str]: (성공 여부, 디렉토리 경로)
    """
    try:
        # 1. DB에서 디렉토리 정보 조회
        upload_dir = await get_upload_dir_from_db(db_name, chat_bot_id)
        if not upload_dir:
            logger.error("PDF 업로드 디렉토리 정보를 찾을 수 없습니다.")
            return False, ""

        # 2. 전체 경로 생성
        full_path = os.path.join(Config.VIEWER_PDF_DIR, upload_dir)

        # 3. 디렉토리 확인 및 생성
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            logger.info(f"PDF 업로드 디렉토리 생성 완료: {full_path}")

        return True, full_path

    except Exception as e:
        logger.error(f"PDF 업로드 디렉토리 처리 중 오류 발생: {e}")
        return False, ""

async def get_or_create_prompt_dir(db_name: str, chat_bot_id: str) -> Tuple[bool, str]:
    """
    챗봇 별 프롬프트 디렉토리 정보를 조회하고, 없으면 생성합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 챗봇 ID

    Returns:
        Tuple[bool, str]: (성공 여부, 디렉토리 경로)
    """
    try:
        # 1. DB에서 디렉토리 정보 조회
        prompt_dir = await get_upload_dir_from_db(db_name, chat_bot_id)
        if not prompt_dir:
            logger.error("프롬프트 디렉토리 정보를 찾을 수 없습니다.")
            return False, ""

        # 2. 전체 경로 생성
        full_path = os.path.join(Config.PROMPT_DIR, prompt_dir)
        prompt_check = os.path.join(full_path, "main_chain.yaml")
        logger.info(f"프롬프트 디렉토리 경로: {full_path}")
        # 3. 디렉토리 확인 및 생성
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            logger.info(f"업로드 디렉토리 생성 완료: {full_path}")
        if not os.path.exists(prompt_check):
            default_prompt = os.path.join(Config.PROMPT_DIR, "main_default_inst.yaml")
            shutil.copy2(default_prompt, prompt_check)
            logger.info(f"기본 프롬프트 파일 복사 완료: {prompt_check}")

        return True, full_path

    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"업로드 디렉토리 처리 중 오류 발생: {e}\n{error_trace}")
        return False, ""


async def check_duplicate_contents(
    contents: List[str], table_name: str, db_name: str
) -> tuple[bool, List[str]]:
    """
    학습하려는 컨텐츠들이 이미 학습되어 있는지 확인합니다.

    Args:
        contents: 학습하려는 컨텐츠 리스트
        table_name: 테이블 이름
        db_name: 데이터베이스 이름

    Returns:
        tuple: (중복 여부, 중복된 컨텐츠 리스트)
    """
    try:
        # content 목록을 IN 절로 한 번에 조회
        query = f"""
            SELECT DISTINCT content
            FROM {table_name}
            WHERE content = ANY($1)
        """
        result = await execute_query(
            query, params=(contents,), fetch=True, dbname=db_name
        )

        # 디버깅용 로깅
        logger.info(f"중복 검사 결과: {result}")

        # 중복된 컨텐츠 추출
        if result:
            duplicate_contents = [row["content"] for row in result]
            return True, duplicate_contents

        return False, []

    except Exception as e:
        logger.error(f"중복 검사 중 오류 발생: {e}")
        raise RuntimeError(f"중복 검사 중 오류 발생: {e}")



async def get_model_name(db_name, chat_bot_id):
    # 업로드 디렉토리 확보
    success, upload_dir_full = await get_or_create_upload_dir(db_name, chat_bot_id)
    if not success or not upload_dir_full:
        logger.error("업로드 디렉토리 정보를 찾을 수 없습니다.")
        raise Exception("Failed to get upload directory.")

    # 상대 경로 계산
    try:
        relative_upload_dir = os.path.relpath(upload_dir_full, Config.UPLOAD_FOLDER)
    except Exception as e:
        logger.error(f"상대 경로 계산 실패: {e}")
        raise

    # account_setting.yaml 전체 경로
    account_file_path = os.path.join(
        Config.ACCOUNT_DIR, relative_upload_dir, "account_setting.yaml"
    )

    model_name = get_ai_config().policy("defaults")["setting_model"]

    logger.info("===============================================================")
    logger.info(f"account_file_path: {account_file_path}")
    logger.info("===============================================================")

    # account_setting.yaml 파싱 시도
    if os.path.exists(account_file_path):
        try:
            with open(account_file_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                model_name = config.get("model_name", model_name)
                logger.info(f"{type(config)} : {config}")
                logger.info(
                    f"account_setting.yaml 로드 완료: model_name = {model_name}"
                )
        except Exception as e:
            logger.warning(f"account_setting.yaml 파싱 실패, 기본 모델 사용: {e}")
            model_name = get_ai_config().policy("defaults")["setting_model"]
    else:
        logger.warning(
            f"account_setting.yaml 없음: {account_file_path}, 기본 모델 사용"
        )
        model_name = get_ai_config().policy("defaults")["setting_model"]

    return get_ai_config().setting_model(model_name)


async def get_or_create_web_content_dir(db_name: str, chat_bot_id: str) -> Tuple[bool, str]:
    """
    웹 컨텐츠 디렉토리 정보를 조회하고, 없으면 생성합니다.
    Postgres에서 사용자의 업로드 경로를 얻어와 웹 컨텐츠 디렉토리를 반환 혹은 생성합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 챗봇 ID

    Returns:
        Tuple[bool, str]: (성공 여부, 디렉토리 경로)
    """
    try:
        # 1. DB에서 디렉토리 정보 조회
        upload_dir = await get_upload_dir_from_db(db_name, chat_bot_id)
        if not upload_dir:
            logger.error("업로드 디렉토리 정보를 찾을 수 없습니다.")
            return False, ""

        # 2. 전체 경로 생성
        full_path = os.path.join(Config.WEB_CONTENT_DIR, upload_dir)

        # 3. 디렉토리 확인 및 생성
        if not os.path.exists(full_path):
            os.makedirs(full_path, exist_ok=True)
            logger.info(f"웹 컨텐츠 디렉토리 생성 완료: {full_path}")

        return True, full_path

    except Exception as e:
        logger.error(f"웹 컨텐츠 디렉토리 처리 중 오류 발생: {e}")
        return False, ""


CHATBOT_DB_CACHE_KEY_PREFIX = "chatbot:db_mapping"
CHATBOT_DB_CACHE_TTL = 60 * 60 * 24  # 24시간


async def get_db_name_by_chatbot_id(chat_bot_id: str) -> Optional[str]:
    """
    chat_bot_id로 db_name을 조회합니다. (Redis 캐싱 적용)
    chatty DB의 chatbot_total_manager 테이블을 기준으로 조회합니다.
    """
    try:
        cache_key = f"{CHATBOT_DB_CACHE_KEY_PREFIX}:{chat_bot_id}"
        redis_client = await get_redis()

        cached = await redis_client.get(cache_key)
        if cached:
            logger.info("chatbot db_name 캐시 조회 성공: chat_bot_id=%s db_name=%s", chat_bot_id, cached)
            return cached

        logger.info("chatbot db_name 캐시 미스: chat_bot_id=%s cache_key=%s", chat_bot_id, cache_key)

        conn = await connect_db("chatty")
        try:
            query = """
                SELECT db_name
                FROM chatbot_total_manager
                WHERE chat_bot_id = $1
            """
            db_name = await conn.fetchval(query, chat_bot_id)
            if db_name:
                await redis_client.set(cache_key, db_name, ex=CHATBOT_DB_CACHE_TTL)
                logger.info("chatbot db_name DB 조회 성공: chat_bot_id=%s db_name=%s", chat_bot_id, db_name)
                return db_name
        finally:
            await return_connection(conn, "chatty")

        logger.error("chatbot db_name 조회 실패: chat_bot_id=%s cache_key=%s table=chatbot_total_manager", chat_bot_id, cache_key)
        return None
    except Exception as e:
        logger.error("get_db_name_by_chatbot_id 예외: chat_bot_id=%s cache_key=%s detail=%s", chat_bot_id, cache_key, e)
        raise


async def check_chatbot_id_valid(chat_bot_id: str) -> str:
    """
    chat_bot_id의 유효성을 검증하고 db_name을 반환합니다.
    """
    try:
        db_name = await get_db_name_by_chatbot_id(chat_bot_id)
        if not db_name:
            raise ValueError("Chatbot ID is not valid")
        return db_name
    except Exception as e:
        raise ValueError(f"Chatbot ID is not valid: {e}")
