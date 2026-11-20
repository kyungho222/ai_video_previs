# 기존 whoami에서 무중단 모델 설정 구조로 전환

모델명·옵션·키 선택을 코드에 직접 적어 두었거나 시작 시 고정한 서비스를 중앙 DB·runtime 기반으로 전환하는 절차입니다. 실제 파일은 [README](00_README.md), 계정 선택 상세는 [API 키 가이드](02_API_KEY_GUIDE.md)를 참고합니다.

## 1. 전환 후 동작과 무중단 범위

| 항목 | 전환 시 확인할 기존 구현 | 현재 구조 |
| --- | --- | --- |
| 모델 선택 | 모델명별 분기·정적 목록·직접 SDK 생성 | 등록 식별자·별칭·API 모델 ID를 해석해 Provider 실행 경로 선택 |
| 설정 원본 | Python 상수 또는 서비스별 JSON | 관리자 DB. 서비스는 발행된 `runtime.json` 사용 |
| 변경 반영 | 객체·상수를 시작 시 만들었다면 코드 교체·재시작 필요 | 새 설정 조회·모델 생성에서 새 버전 반영 |
| 진행 중 요청 | 설정 재조회 시 버전 혼합 가능성 확인 | snapshot 범위 안에서는 동일 버전 유지 |
| API 키 | 서비스별 고정 키·별도 사용자 분기 | 최종 모델의 지정 인증 우선, 기본 OpenAI 경로는 사용자 계정 선택 |

같은 API 규격의 모델 등록·활성 상태·옵션·주소·기존 환경변수를 가리키는 인증 설정 등은 해당 호출부가 공통 설정을 읽으면 재시작 없이 반영할 수 있습니다. 코드·SDK·프로세스 환경변수의 실제 값·새 Provider adapter 교체는 배포·재시작이 필요한 범위입니다.

기존 파일을 바꾸는 최초 배포 자체는 이 모듈이 무중단으로 만들어 주지 않습니다. 연결을 유지해야 하는 환경은 새 인스턴스·worker를 준비하고 확인한 뒤 트래픽을 순차 전환하며, 진행 중 작업을 정리하는 기존 배포 절차를 사용합니다. 단일 프로세스 정지·기동을 무중단으로 보고하지 않습니다.

## 2. 작업 시작 전 조사

대상 서비스의 최신 `dev`에서 작업 브랜치를 만듭니다. 전용 기능과 기존 정상 코드·runtime·환경 설정의 복구용 사본을 확보한 뒤 다음을 확인합니다.

- `whoami` import 위치와 사용 함수. 루트·`utils` 외 패키지 경로, `import *`, SDK 클래스 상속·타입 검사 포함.
- 사용자 선택 모델뿐 아니라 언어 판별·키워드·웹검색·추천 질문·요약·fallback에 사용하는 모든 모델.
- 시작 시 보관하는 모델 상수, SDK 객체, 모델명만 키로 사용하는 client 캐시, 직접 SDK·HTTP 생성.
- app 외 Celery worker·스케줄러·배치 및 이미지·영상·음성 등 실제 사용하는 기능별 실행기.
- 서비스 고유 Config 경로·DB helper·RunPod URL 조회·로그·Redis 연결.

대상 서비스 루트에서 조사하는 예시입니다.

```bash
rg -n 'from .*whoami|import whoami|get_model_by_name|build_openai_chat_model|ChatOpenAI|LOCAL_MODEL_MAP|SLLM_MODEL_MAP|isinstance' .
```

같은 `WHOAMI_VERSION` 문자열만으로 파일이 같다고 판단하지 않습니다. 이 묶음의 복사본은 Chatty 기준이며, 다른 서비스의 경로·RunPod registry·DB helper 차이는 유지해야 합니다.

## 3. 중앙 DB와 환경 준비

중앙 관리 서버 담당자가 `chatbot_model_config`, `chatbot_model_config_common`과 일관된 메타데이터 버전, 실제 호출할 활성 모델을 준비합니다. 소비 서비스의 whoami는 테이블 생성·초기 등록을 수행하지 않습니다. 모델 전용 서비스도 현재 로더가 요구하는 전체 설정 묶음을 읽으므로 임의로 chat 항목만 남기지 않습니다.

| 환경 | 준비할 값 |
| --- | --- |
| `MARIA_DB_HOST`, `MARIA_DB_PORT` | 중앙 MariaDB 연결 주소·포트 |
| `MARIA_DB_USER`, `MARIA_DB_PASSWORD` | 설정 및 필요한 계정 선택 조회 권한을 가진 계정 |
| `MODEL_REGISTRY_DB` | 중앙 모델 관리 DB 이름. 기본 `chatty` |
| `AI_CONFIG_DIR` | 실행용 runtime·잠금·임시 파일을 둘 절대 경로 |
| `MODEL_REGISTRY_REFRESH_SECONDS` | 기본 `30`. 유한한 0 이상 숫자. `0`은 백그라운드 확인 중지 |
| Provider API 키 | 실제 등록 모델이 참조하는 환경변수. 사용자별 OpenAI 키는 별도 상세 가이드 참조 |

환경변수가 없는 DB 항목은 `config.Config`로 보충하며 빈 문자열은 누락과 다릅니다. `.env` 로드는 서비스 시작 코드나 실행 환경이 담당합니다. 서비스 고유 `config.py`를 Chatty 파일로 덮어쓰지 않습니다.

사용자별 계정 선택은 `chatty.chatty_apply_list`를 명시적으로 조회합니다. `MODEL_REGISTRY_DB`만 바꿔도 이 테이블 경로가 바뀌지는 않습니다. 정상 모델 runtime이 있어도 별도 사용자 DB·검색 DB·계정 조회 연결까지 보장되는 것은 아닙니다.

Linux/WSL의 서비스 실행 계정이 runtime 디렉터리에 파일을 만들고 읽고 쓸 수 있어야 합니다. `.runtime.lock`의 `fcntl.flock`, 같은 디렉터리의 임시 파일 생성·원자적 rename을 지원해야 합니다. 새 runtime 기본 권한은 `0640`이며 기존 runtime 또는 seed 파일 권한을 물려받을 수 있습니다. 공유 경로는 그룹 권한도 확인합니다.

Docker 코드 경로가 읽기 전용이면 쓰기 가능한 별도 볼륨을 `AI_CONFIG_DIR`로 연결합니다. 서로 다른 서버의 로컬 경로 문자열이 같다고 실제 파일이 공유되는 것은 아닙니다.

필요 의존성은 사용하는 Provider SDK와 `PyMySQL`입니다. 로컬 health probe는 `aiohttp`, RunPod 모델은 해당 서비스의 주소 조회 모듈이 추가로 필요합니다. `whoami_db.py`에는 별도로 `config`, `logs`, `db`, YAML, Redis·PostgreSQL 관련 기존 의존성이 필요합니다. 대상 서비스의 의존성 관리 방식으로 준비합니다.

## 4. 파일 비교와 호출부 전환

1. [whoami.py](utils/whoami.py)를 기존 import 위치에 비교 적용합니다. `utils/whoami.py`와 루트 `whoami.py` 배치를 지원합니다. 다른 패키지 배치는 기본 설정 경로를 맞추거나 `AI_CONFIG_DIR`를 명시합니다.
2. DB·업로드 도우미를 사용하는 경우에만 [whoami_db.py](utils/whoami_db.py)를 비교 적용합니다. 기존 함수 계약·서비스 경로·DB 연결을 보존합니다. 모델 생성만 사용하면 이 파일이 필요하지 않습니다.
3. 한 프로세스에서 같은 파일을 `whoami`와 `utils.whoami`로 중복 import하지 않습니다. 모듈별 캐시·snapshot이 분리될 수 있습니다.
4. 호출 시점에 공통 모델 생성 함수를 사용하고 실제 사용자 `db_name`을 전달합니다. 반환 tuple과 동기·비동기 계약을 맞춥니다.

```python
from utils.whoami import get_model_by_name, build_openai_chat_model

async def make_answer_model(model_name, db_name, chat_bot_id):
    client, resolved_name = await get_model_by_name(
        model_name, temperature=0, db_name=db_name, chat_bot_id=chat_bot_id,
    )
    return client, resolved_name

def make_openai_helper(model_name, db_name, chat_bot_id):
    return build_openai_chat_model(
        model_name=model_name, temperature=0, db_name=db_name,
        chat_bot_id=chat_bot_id,
    )
```

`get_model_by_name()`은 async이며 `(SDK 객체, resolved_name)`을 반환합니다. OpenAI helper는 동기 함수입니다. 이전에 임의 이름으로 호출되던 모델도 현재는 등록·활성 상태와 지원 Provider 검증을 통과해야 합니다. 호출부가 명시한 fallback 정책은 별도로 확인합니다.

SDK 호환 export와 `LocalModel()`·`SLLMModel()`은 원본 SDK 클래스 자체가 아니라 생성 함수일 수 있습니다. `isinstance`, 상속, 클래스 이름으로 Provider·timeout을 판정하던 코드를 조사하고, 같은 snapshot의 등록 설정으로 판정하도록 연결합니다.

시작 시 import한 모델명·tuple·`LOCAL_MODEL_MAP`·`SLLM_MODEL_MAP` 복사본과 기존 SDK 객체는 자동 갱신되지 않습니다. 영구 캐시를 없애거나 필요한 설정 버전에 맞춰 다시 생성합니다. 진행 중 요청은 기존 객체를 유지하고 후속 요청이 새 객체를 사용하게 합니다.

## 5. 요청과 worker의 설정 snapshot

개별 공통 생성 함수에는 snapshot 처리가 적용되어 있습니다. 하나의 요청에서 모델 판정·여러 생성·fallback·스트리밍까지 같은 버전을 쓰려면 요청 전체 범위를 묶습니다.

FastAPI의 기존 `app`에 적용하는 예시입니다. 서비스의 미들웨어 순서·기동 조건을 함께 확인합니다. 이 미들웨어는 일반 HTTP 요청에서도 최초 설정 조회를 수행할 수 있으므로 첫 요청 전에 DB/runtime을 준비합니다.

```python
from utils.whoami import ModelConfigMiddleware

app.add_middleware(ModelConfigMiddleware)
```

worker·배치는 작업마다 snapshot을 열고 작업이 끝나면 닫습니다. worker의 무한 루프 전체를 하나의 snapshot으로 감싸지 않습니다.

```python
from utils.whoami import model_config_snapshot, get_model_by_name

async def run_one_job(model_name, db_name, chat_bot_id, messages):
    with model_config_snapshot():
        client, _ = await get_model_by_name(
            model_name, db_name=db_name, chat_bot_id=chat_bot_id,
        )
        return await client.ainvoke(messages)
```

SDK 스트림을 호출자에게 돌려주는 함수에서 snapshot을 먼저 닫지 않도록 합니다. `async for`로 청크를 소비하는 시점까지 범위를 유지합니다. snapshot은 모델 설정 버전을 고정하는 기능이며 외부 API 장애·DB 작업 전체를 보호하는 기능은 아닙니다. API 계정 선택의 60초 캐시는 별도입니다.

## 6. 최초 기동과 이후 설정 변경

| 상황 | 동작 |
| --- | --- |
| runtime 파일 없음 | 최초 설정 조회에서 DB를 동기적으로 읽어 검증 후 runtime 생성 |
| 정상 runtime 있음 | 로컬 설정으로 처리. 별도 서버는 확인 간격이 지난 설정 조회에서 DB 버전 확인 예약 |
| 같은 runtime 경로 공유 | 관리자 발행·원자적 교체 후 후속 조회에서 파일 변경 감지 |
| 별도 서버·로컬 runtime | DB 버전이 바뀌면 전체 설정을 가져와 검증·발행하고 후속 조회에서 반영 |
| 진행 중 요청·작업 | snapshot을 열어 두었다면 선택한 버전으로 계속 처리 |

기본 30초는 주기적으로 혼자 실행되는 갱신 타이머가 아닙니다. **확인 간격이 지난 다음 설정 조회**가 백그라운드 확인을 시작합니다. 그 요청은 기존 snapshot으로 계속 처리할 수 있고 유휴 서비스는 확인하지 않습니다. DB의 두 메타데이터 버전은 서로 같아야 하고, 기존 runtime과 다른 버전이어야 변경을 감지합니다. 모델 행만 SQL로 바꾸고 버전을 그대로 두는 방식을 일반 운영에 사용하지 않습니다.

`MODEL_REGISTRY_REFRESH_SECONDS=0`이어도 runtime이 없으면 최초 DB 읽기는 수행합니다. 음수·NaN·무한대·문자열 오류는 허용하지 않습니다.

최초 배포에서는 검증한 코드·필요 호출부·SDK·환경을 app과 worker에 반영하고 해당 프로세스를 재시작합니다. 이후 지원하는 설정만 관리자에서 변경할 때는 재시작 없이 새 요청 반영을 확인합니다. 새 환경변수의 실제 값을 실행 프로세스에 추가하는 작업은 설정 JSON 변경과 구분합니다.

## 7. 검증 절차

### 정적 점검

다음은 대상 서비스의 `utils` 배치 예시입니다. 루트 배치라면 경로·import를 바꾸고 DB 도우미를 적용하지 않는 서비스는 해당 파일을 목록에서 제외합니다. 이 검사는 DB·모델 API를 호출하지 않습니다.

```bash
python3 -B - <<'PY'
import ast
from pathlib import Path
for name in ('utils/whoami.py', 'utils/whoami_db.py'):
    ast.parse(Path(name).read_text(), filename=name)
from utils import whoami
print('module:', whoami.__file__)
print('runtime directory:', whoami.model_config_directory())
PY
```

### 최초 DB 읽기와 필수 모델 조회

아래는 대상 환경에서 실행하는 **DB 접근·runtime 발행 가능** 점검입니다. SDK 객체나 유료 모델 호출은 하지 않습니다. runtime이 없으면 생성하므로 실행 계정·환경·경로를 먼저 준비합니다.

```bash
python3 -B - <<'PY'
from utils.whoami import get_chat_config, model_config_directory
config = get_chat_config()
print('runtime directory:', model_config_directory())
print('loaded version:', config.version)
# 대상 서비스에서 실제로 쓰는 등록 식별자로 바꾼다.
settings, api_model, _ = config.resolve_model('대상_서비스_필수_모델')
print('required model:', settings['provider'], api_model)
PY
```

짧은 단발 명령은 백그라운드 갱신 완료 검증이 아닙니다. 살아 있는 서비스에서 다음 항목을 확인합니다.

| 검증 | 완료 기준 |
| --- | --- |
| 최초 기동 | DB bootstrap 또는 기존 runtime 조회 성공, 필수 모델·보조 모델 등록·활성 확인 |
| 기능 회귀 | 채팅·검색·요약·업로드·worker 등 실제 적용 범위가 기존대로 동작 |
| 설정 갱신 | 테스트 환경의 안전한 관리자 변경 후 후속 요청의 새 버전과 실제 생성 옵션 확인 |
| 진행 중 요청 | 기존 스트리밍/작업의 snapshot은 이전 버전 유지, 이후 요청은 새 버전 사용 |
| 복수 서버·worker | 각각 새 설정을 관찰하는지 확인. 경로 이름이 같은 것만으로 공유라고 판단하지 않음 |
| API 계정 | 별도 상세 가이드의 1·2·3, 지정 인증, 누락·조회 실패, 60초 캐시 검증 |

`[whoami] applied settings`, 별도 서버의 `[whoami] registry refresh applied` 로그와 실제 로드 버전을 확인합니다. 일반 기능 검증에서 모델 API를 호출하면 비용이 발생할 수 있으므로 대상 환경의 테스트 절차에 따릅니다. 키·비밀번호·전체 설정의 민감 값을 로그로 출력하지 않습니다.

## 8. 장애와 복구

| 상황 | 현재 동작과 대응 |
| --- | --- |
| runtime 없고 DB 실패 | 최초 조회·기동 실패. DB·schema·등록 모델·권한부터 확인. seed 자동 fallback 없음 |
| 정상 runtime 있고 백그라운드 DB 확인 실패 | 정상 모델 설정 유지. 이후 조회에서 재시도. 다른 DB 작업 성공까지 보장하지 않음 |
| 실행 중 잘못된 runtime으로 교체, 이전 정상 캐시 있음 | 해당 프로세스는 마지막 정상 설정 유지 |
| 재시작 후 첫 runtime이 잘못됨 | 이전 정상 메모리 캐시가 없으므로 실패. 파일·설정 원인 확인 |
| 새 설정이 반영되지 않음 | 등록·버전, 실제 경로, 프로세스 환경, 확인 간격·호출 발생, 영구 SDK 캐시 확인 |

정상 runtime을 먼저 삭제하거나 `python whoami.py --apply`로 구형 seed를 덮어쓰는 방법을 일반 복구 절차로 사용하지 않습니다. 해당 CLI는 seed 문서 검사·수동 발행 경로이며 운영 DB/runtime 상태 점검과 다릅니다.

코드 롤백은 호환되는 공통 파일과 필요한 호출부를 함께 되돌리고 배포 절차로 재시작합니다. DB/runtime 설정 복원은 별도 작업이며 관리자 수정 이력·버전을 함께 확인합니다. 파일만 과거 버전으로 바꿔도 이후 DB 자동 확인에서 다시 갱신될 수 있습니다. 공유 `dev`를 reset·강제 push해 복구하지 않습니다.

## 9. 추가 연동 범위와 완료 보고

공통 파일 교체가 자동으로 이관하지 않는 항목도 확인합니다.

- 새 API 규격·Provider adapter·SDK 기능, 미디어 생성·조회·다운로드와 기능별 지원 조건.
- 관리자 API·모델 목록 조회·프런트 화면. 일반 소비 서비스에는 관리자 전체 코드를 복사하지 않습니다.
- 최종 답변의 공통 텍스트 정규화. 대상 출력부가 `extract_text_content()`를 사용하고 사용량 메타데이터를 보존하는지 확인합니다. whoami의 기존 SDK 호환 처리만으로 모든 출력부가 변경되지는 않습니다.
- 채팅 기록·사용량·키워드 확정 저장. 대상 서비스의 기존 저장 호출·테이블·후속 API를 보존하며, whoami 두 파일만으로 새 저장 기능이 생긴다고 가정하지 않습니다.

기준 커밋, 변경 파일·보존한 전용 함수, import 경로, 중앙 DB·runtime 경로·공유 여부, 실행 계정·권한·확인 간격, 최초 배포·재시작 결과, 기능·설정 갱신·snapshot·API 계정 검증 결과와 남은 미적용 경로를 전달합니다.
