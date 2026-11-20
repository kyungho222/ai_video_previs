import logging
from logs.logging_util import LoggerSingleton
from config import Config
from typing import Optional, Tuple
from db.db_operations import execute_query, delete_data, insert_data, update_data
import os
from typing import Optional, List
import shutil
import traceback
import yaml
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_deepseek import ChatDeepSeek
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_xai import ChatXAI
from langchain_mistralai import ChatMistralAI
from langchain_ollama import OllamaLLM
from langchain_naver import ChatClovaX
from langchain_qwq import ChatQwen

class ChatKimiK2(ChatOpenAI):
    """키미K2 클래스 (MOONSHOT_API_KEY 사용)"""
    def __init__(self, model="kimi-k2-0905-preview", **kwargs):
        import os
        moonshot_key = os.getenv("MOONSHOT_API_KEY")
        if moonshot_key:
            kwargs['api_key'] = moonshot_key
            kwargs['base_url'] = "https://api.moonshot.ai/v1"
        super().__init__(model=model, **kwargs)

logger = LoggerSingleton.get_logger(
    logger_name="chat.faiss_process", level=logging.INFO
)


async def get_chat_id_from_db(db_name, chat_bot_id):
    """
    주어진 db_name의 chatbot_setup 테이블에서 chat_bot_id에 해당하는 chat_id를 반환합니다.

    Args:
        db_name (str): 데이터베이스 이름
        chat_bot_id (str): 조회할 챗봇의 chat_bot_id

    Returns:
        str: chat_id (존재하지 않으면 None 반환)
    """
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
        logger.error(f"chat_id 조회 실패: {e}")
        return None


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

    model_name = "gpt-4.1"

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
            model_name = "gpt-4.1"
    else:
        logger.warning(
            f"account_setting.yaml 없음: {account_file_path}, 기본 모델 사용"
        )
        model_name = "gpt-4.1"

    # LLM 구성
    if model_name == "gpt":
        model_name = "gpt-4.1"
    elif model_name == "claude":
        model_name = "claude-3-5-haiku-latest"
    elif model_name == "deepseek":
        model_name = "deepseek-chat"
    elif model_name == "gemini" or model_name == "google":
        model_name = "gemini-2.5-flash"
    elif model_name == "xai" or model_name == "grok":
        model_name = "grok-3-fast-beta"
    elif model_name == "mistral":
        model_name = "mistral-large-latest"
    elif model_name == "clova" or model_name == "naver":
        model_name = "HCX-005"
    elif model_name == "qwen3" or model_name == "qwen" or model_name == "qwen-3":
        model_name = "qwen3-max"
    elif model_name in ["kimik2", "kimi", "kimi-k2", "Kimi-K2-Instruct", "kimi-k2-pro", "kimik2-pro"]:
        model_name = "kimi-k2-0905-preview"

    return model_name


async def get_model_by_name(model_name, temperature=0):
    if model_name.startswith("gpt-5"):
        return ChatOpenAI(
            model_name=model_name,
            temperature=0
        ), "gpt-5-chat-latest"
    elif model_name.startswith("gpt-"):
        return ChatOpenAI(model_name=model_name, temperature=0), model_name
        # return ChatOpenAI(model_name="gpt-4.1", temperature=0), "gpt-4.1"
    elif model_name == "gpt":
        return ChatOpenAI(model_name="gpt-4.1", temperature=0), "gpt-4.1"
    elif model_name == "claude-sonnet-4":
        return ChatAnthropic(model_name ="claude-sonnet-4-20250514"), model_name
    elif model_name == "claude":
        return ChatAnthropic(model_name="claude-3-5-haiku-latest", temperature=0), "claude-3-5-haiku-latest"
    elif model_name.startswith("claude-"):
        return ChatAnthropic(model_name=model_name, temperature=0), model_name
    elif model_name == "deepseek":
        return ChatDeepSeek(model_name="deepseek-chat", temperature=0), "deepseek-chat"
    elif model_name.startswith("deepseek-"):
        return ChatDeepSeek(model_name=model_name, temperature=0), model_name
    elif model_name.startswith("gemini-"):
        return ChatGoogleGenerativeAI(model=model_name, temperature=0), model_name
    elif model_name == "gemini" or model_name == "google":
        return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0), "gemini-2.5-flash"
    elif model_name == "xai" or model_name == "grok":
        return ChatXAI(model="grok-3-fast-beta", temperature=0), "grok-3-fast-beta"
    elif model_name.startswith("grok-"):
        return ChatXAI(model=model_name, temperature=0), model_name
    elif model_name.startswith("mistral-"):
        return ChatMistralAI(model=model_name, temperature=0), model_name
    elif model_name == "mistral":
        return ChatMistralAI(model="mistral-large-latest", temperature=0), "mistral-large-latest"
    elif model_name == "clova" or model_name == "naver":
        return ChatClovaX(model="HCX-005", temperature=0), "HCX-005"
    elif model_name.startswith("HCX-"):
        return ChatClovaX(model=model_name, temperature=0), model_name
    elif model_name.startswith("qwen3-") or model_name.startswith("qwen-3"):
        # qwen-3-max → qwen3-max로 변환
        if model_name.startswith("qwen-3"):
            normalized_model = model_name.replace("qwen-3", "qwen3")
        else:
            normalized_model = model_name
        return ChatQwen(model=normalized_model, temperature=temperature), normalized_model
    elif model_name == "qwen3" or model_name == "qwen" or model_name == "qwen-3":
        return ChatQwen(model="qwen3-max", temperature=temperature), "qwen3-max"
    elif model_name in ["kimik2", "kimi", "kimi-k2", "Kimi-K2-Instruct", "kimi-k2-pro", "kimik2-pro"]:
        return ChatKimiK2(model="kimi-k2-0905-preview", temperature=temperature), "kimi-k2-0905-preview"
    elif model_name.startswith("kimik2-") or model_name.startswith("kimi-k2"):
        return ChatKimiK2(model="kimi-k2-0905-preview", temperature=temperature), "kimi-k2-0905-preview"

    elif model_name == "llama":
        # llm = OllamaLLM(
        #     model = "lancard/korean-yanolja-eeve",
        #     base_url = "http://218.146.11.93:11434"
        # )

        llm = ChatOpenAI(model = "lancard/korean-yanolja-eeve" , base_url="http://218.146.11.93:11434", temperature=0)
        return llm, "llama"
    else:
        return ChatOpenAI(model_name="gpt-4.1", temperature=0), "gpt-4.1"

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
