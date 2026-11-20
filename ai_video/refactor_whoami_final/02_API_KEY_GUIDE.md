# OpenAI API 키 분기 이관 가이드

다른 서비스에 `refactor/whoami`의 사용자별 OpenAI 계정 선택을 적용하는 작업자를 위한 묶음입니다. `chatty.chatty_apply_list.api_account_server`를 기준으로 계정을 선택하며, 실제 API 키는 대상 서비스의 환경변수에서 읽습니다.

2026-10-29 기준 파일을 복사했습니다. API 계정 분기를 추가한 커밋은 `6e2fe52d`이며, 전체 복사본은 `refactor/whoami`의 `d66bf9e3` 기준입니다. 대상 서비스의 원본과 diff를 비교해 적용합니다. 실행 서버에 배포되었다는 의미는 아닙니다.

전체 전환 순서는 [기존 whoami에서 무중단 설정 구조로 전환](01_MIGRATION_GUIDE.md)을 먼저 읽습니다. 이 문서는 그중 API 계정 선택의 상세 설명입니다.

## 포함 파일과 적용 범위

| 파일 | 용도 |
| --- | --- |
| [utils/whoami.py](utils/whoami.py) | 공통 모델 설정·SDK 객체 생성·API 계정 분기. 대상 서비스의 기존 import 위치에 비교 적용 |
| [utils/whoami_db.py](utils/whoami_db.py) | Chatty의 DB·업로드·프롬프트 도우미 참고본. 해당 도우미가 필요한 경우에만 기존 구현과 비교 적용 |
| [04_MESSAGE.md](04_MESSAGE.md) | 다른 작업자에게 그대로 전달할 작업 요청 메시지 |

`whoami.py`는 API 키 분기만 추린 패치가 아니라 현재 공통 설정 로더 전체입니다. 모델 등록 검증·runtime 갱신·SDK 호환 처리도 포함합니다. 대상 서비스에 전용 함수가 있다면 보존하며 비교 적용합니다.

이 문서의 사용자별 API 계정 분기는 OpenAI 채팅 모델과 공통 OpenAI helper 경로에 적용됩니다. 이미지·영상의 자체 SDK/HTTP 호출, 기능별 라우터, 관리자 서버 전체를 자동 이관하지 않습니다. 같은 DB 계정 분기가 모든 공급사와 미디어 호출에 적용된다고 가정하지 않습니다.

## 계정 선택 규칙

공통 `ChatOpenAI` 생성 경로에서 다음 순서로 결정합니다.

1. 모델의 최종 설정에 `credential_env` 또는 `credential_profile`이 있으면 지정 인증을 사용합니다. Provider 공통 설정에서 상속된 인증도 포함합니다. 둘 다 있으면 `credential_env`가 마지막에 적용됩니다.
2. 지정 인증이 없으면 `db_name`을 사용자 ID로 사용해 아래 조회를 실행합니다.
3. 조회 결과가 없거나 NULL·지원하지 않는 값이거나 조회가 실패하면 runtime의 `tenant_groups` 정책으로 결정합니다.

```sql
SELECT api_account_server
FROM chatty.chatty_apply_list
WHERE user_id = %s
LIMIT 1;
```

| api_account_server | 인증 프로필 | 환경변수 |
| --- | --- | --- |
| 1 | `openai_default` | `OPENAI_API_KEY` |
| 2 | `openai_second` | `OPENAI_SECOND_API_KEY` |
| 3 | `openai_asadal` | `OPENAI_ASADAL_API_KEY` |

계정 선택 결과는 프로세스별 60초간 캐시합니다. 조회 실패·미설정 결과도 캐시하며 API 키 원문은 이 캐시에 저장하지 않습니다. DB 값을 변경해도 다음 조회에 즉시 반영되지 않을 수 있고, 이미 만들어 재사용하는 SDK 객체의 키는 자동 교체되지 않습니다.

DB가 1·2·3 중 하나를 지정했는데 해당 환경변수가 비어 있으면 오류를 발생시킵니다. 이 경우 다른 계정 키로 자동 대체하지 않습니다. 기존 사용자 그룹 fallback은 `factory`와 `helper`에서 각각 `factory_second`, `second` 그룹을 사용하는 차이가 있으므로 현재 정책을 보존합니다.

## 대상 서비스 준비

- 실제 사용하는 SDK와 `PyMySQL`을 대상 서비스의 기존 의존성 관리 방식으로 준비합니다. 이 묶음이 패키지를 자동 설치하지는 않습니다.
- `MARIA_DB_HOST`, `MARIA_DB_PORT`, `MARIA_DB_USER`, `MARIA_DB_PASSWORD`를 서비스 프로세스에 전달합니다. 환경변수가 없는 항목은 기존 `config.Config`에서 보충하지만 빈 문자열은 없는 값으로 처리하지 않습니다.
- `MODEL_REGISTRY_DB`는 모델 관리 DB 이름이며 기본값은 `chatty`입니다. 계정 선택 쿼리의 `chatty.chatty_apply_list`는 코드에 명시된 이름이므로 이 환경변수만 바꿔도 조회 대상이 바뀌지는 않습니다. 동일 연결 계정으로 접근 가능한지 확인합니다.
- 사용할 OpenAI 계정 키를 서비스 프로세스에 전달합니다. `whoami.py`는 `.env`를 직접 로드하지 않습니다. 공통 DB·경로·SDK 준비는 [전체 전환 가이드](01_MIGRATION_GUIDE.md)의 환경 설정을 참고합니다.
- 중앙 모델 관리 DB의 `chatbot_model_config`, `chatbot_model_config_common`과 메타데이터가 준비되어 있어야 합니다. 소비 서비스가 테이블을 생성하거나 초기 데이터를 등록하지는 않습니다.
- `AI_CONFIG_DIR`를 서비스 계정이 읽고 쓸 수 있는 절대 경로로 지정합니다. Linux/WSL에서 잠금 파일·임시 파일 생성과 원자적 rename이 가능해야 합니다.

`runtime.json`이 없으면 첫 설정 조회에서 중앙 DB로부터 생성합니다. 정상 runtime이 있으면 유지하며, 기본 30초 간격이 지난 설정 조회에서 백그라운드로 DB 버전을 확인합니다. 소스 `models.json`·`policies.json`이나 다른 서비스의 runtime을 복사해 운영 설정을 덮어쓰지 않습니다. 이 묶음에도 포함하지 않았습니다.

API 계정 선택 캐시의 60초와 모델 설정 갱신의 기본 30초는 별도 동작입니다.

## 적용 순서

1. 대상 서비스의 최신 `dev`에서 작업 브랜치를 만듭니다. `main`에 없는 기반 변경에 의존하는 작업을 `main`에서 별도로 구현하지 않습니다.
2. 기존 공통 파일과 다음 호출부를 비교합니다. 백업·diff 후 서비스 전용 함수와 import 경로를 보존합니다.
3. `whoami.py`를 기존 위치에 적용합니다. `utils/whoami.py`와 루트 `whoami.py` 배치를 지원하지만, 한 프로세스에서 두 이름으로 중복 import하지 않습니다.
4. `whoami_db.py`가 필요한 서비스는 기존 `config`, `logs`, `db`, Redis 및 PostgreSQL 도우미 계약을 비교합니다. 이 참고본을 무조건 덮어쓰지 않습니다. 모델 생성 기능만 사용하면 이 파일은 필요하지 않습니다.
5. 모델 생성 호출에 실제 사용자 `db_name`이 전달되도록 연결합니다. 최종 답변 외에 언어 판별·키워드 추출·웹검색·추천 질문의 생성 경로도 확인합니다.
6. 검증 후 대상 서비스의 app·worker에 필요한 코드·환경을 배포하고 프로세스를 재시작합니다. 코드 파일·환경변수 교체는 runtime 자동 갱신만으로 적용되지 않습니다.

호출부 조사 예시입니다. 대상 서비스 루트에서 실행합니다.

```bash
rg -n 'get_model_by_name|build_openai_chat_model|get_openai_api_key_for_db|ChatOpenAI|OPENAI_API_KEY' .
```

공통 모델 생성 예시는 다음과 같습니다. 이 함수는 SDK 객체를 만들며 실제 답변 생성은 호출부에서 실행합니다.

```python
from utils.whoami import get_model_by_name, build_openai_chat_model

async def create_answer_model(model_name, db_name, chat_bot_id):
    llm, resolved_name = await get_model_by_name(
        model_name,
        temperature=0,
        db_name=db_name,
        chat_bot_id=chat_bot_id,
    )
    return llm, resolved_name

def create_auxiliary_model(model_name, db_name, chat_bot_id):
    return build_openai_chat_model(
        model_name=model_name,
        temperature=0,
        db_name=db_name,
        chat_bot_id=chat_bot_id,
    )
```

모델명은 중앙 registry에 등록·활성화되어 있어야 합니다. 기존에 임의 모델명을 허용하던 서비스도 이 조건을 확인합니다. 호출부가 직접 `ChatOpenAI(api_key=...)`를 생성하거나 다른 HTTP/SDK 클라이언트를 만들면 이 계정 선택 경로를 우회할 수 있습니다.

## 검증과 완료 기준

다음 검증은 대상 서비스의 테스트 환경에서 수행합니다. 새 테스트 데이터를 위해 운영 DB 값을 변경하지 않고 기존 테스트 계정이나 mock을 사용합니다.

| 확인 대상 | 기대 결과 |
| --- | --- |
| 테스트 사용자 값 1·2·3 | 각각 `openai_default`·`openai_second`·`openai_asadal` 선택 |
| 값 없음·NULL·잘못된 값·조회 실패 | 기존 사용자 그룹 정책 적용 |
| DB에서 지정한 계정의 키 누락 | `Missing API account credential` 오류. 다른 계정으로 대체하지 않음 |
| 모델에 지정 인증 있음 | DB 사용자 분기보다 모델의 최종 인증 설정 우선 |
| DB 값 변경 후 새 SDK 생성 | 프로세스별 캐시 만료 후 새로운 선택 반영 |
| 최종 답변·보조 체인 | 올바른 `db_name` 전달 및 공통 생성 경로 사용 |
| 직접 SDK·HTTP 생성 및 재사용 객체 | 계정 분기 우회 여부를 조사하고 필요한 호출부 수정 |

로그의 `[OPENAI API KEY] profile=... db_name=... configured=...` 등 프로필 이름과 설정 여부로 판단합니다. API 키 원문·DB 비밀번호를 로그나 전달 메시지에 출력하지 않습니다. 테스트에서 기대한 프로필인지와 실제 공급사 인증 성공 여부를 구분해 확인합니다.

작업 완료 시 변경 파일, 호출부별 적용 범위, 검증 결과, 누락된 SDK/환경/DB 조건, 배포·재시작 여부를 남깁니다. 모델 생성 기능만 반영했는데 이미지·영상·채팅 저장까지 전체 적용됐다고 보고하지 않습니다.
