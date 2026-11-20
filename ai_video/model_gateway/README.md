# 공용 AI 모델 게이트웨이

프론트의 `model`을 `model_mapping.json`의 별칭으로 조회하고 실제 `api_model`,
공급사, API 주소와 서버 `.env`의 인증 키를 선택합니다. 참고용
`ai_video/model_pipeline_review_20261119`는 실행 시 가져오거나 수정하지 않습니다.

등록되지 않았거나 비어 있는 모델 식별자는 `gpt-6.1-terra`로 전환합니다.
등록된 모델의 키 누락, HTTP 오류, 통신 오류 또는 잘못된 응답은 원인 로그를 남기고
`gpt-6.1-terra`로 한 번 전환합니다. 이후 재시도는 GPT에서만 수행합니다.
원래 모델과 GPT가 모두 실패하면 실패를 반환하며 무한 전환하지 않습니다.
콘텐츠 안전 거절은 다른 모델로 재요청하지 않습니다. 독립 OpenAI moderation 서비스의
오류도 GPT 채팅 호출로 대체하지 않으며 기존 검사 실패 처리를 유지합니다.
`GET /api/chat/models`의 `options`는 중복을 제거한 실제 API 모델값 `value`,
표시명 `label`, 공급사 `provider`, 키 설정 여부 `configured`를 반환합니다.
콘솔은 이 목록으로 시나리오·프리비즈 모델 셀렉트를 구성하고 선택된 `value`를
요청의 `model`로 전달합니다. 키 자체는 응답하지 않습니다.

## 호출 흐름

시나리오 / 프리비즈 / 프롬프트 검사 / 영상 분석
→ `routed_post` → 모델 매핑 → 공급사 API → 공통 응답과 토큰 정보

OpenAI는 Responses API를 유지합니다. 다른 공급사는 Chat Completions 호환 API로
전체 대화, 함수 호출과 결과, 이미지, JSON 출력 요청을 변환합니다. DeepSeek와
Moonshot의 JSON 객체 모드에는 출력 스키마도 시스템 지시로 전달합니다.
기존 파이프라인의 결과 파싱·스키마 검증·보정 흐름은 유지합니다.
모델마다 이미지·함수 호출·구조화 출력 지원 범위가 다르므로 연결 성공과 생성 품질은
각 실제 모델로 별도 검증해야 합니다. 지원하지 않는 입력이나 공급사 오류는
다른 모델로 몰래 전환하지 않고 오류로 반환합니다.

## 환경 변수

| 공급사 | 인증 키 | API 주소 재정의(선택) |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_API_BASE_URL` 또는 `OPENAI_BASE_URL` |
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_BASE_URL` |
| Google | `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY` | `GOOGLE_API_BASE_URL` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_BASE_URL` |
| Qwen | `ALIBABA_API_KEY`, `DASHSCOPE_API_KEY`, `QWEN_API_KEY` 순서 | `QWEN_API_BASE_URL` |
| xAI | `XAI_API_KEY` | `XAI_API_BASE_URL` |
| Mistral | `MISTRAL_API_KEY` | `MISTRAL_API_BASE_URL` |
| Moonshot | `MOONSHOT_API_KEY` 또는 `KIMI_API_KEY` | `MOONSHOT_API_BASE_URL` |

서버 프로젝트 `.env`, 기능 폴더 `.env`, 프로세스 환경 순서로 적용하며 뒤의 값이
우선합니다. 주소를 지정하지 않으면 매핑에 지정된 주소 또는 공급사의 기본 주소를
사용합니다. 프론트가 API 주소나 인증 키를 지정할 수 없습니다. 키가 없으면 해당
공급사 키의 변수명을 안내하며 다른 공급사 키를 대신 사용하지 않습니다.

매핑의 `api_key_name=local`인 사내 모델은 지정된 사내 호환 API에 연결하며
`LOCAL_API_KEY`가 없을 때 참고 코드와 동일한 `LOCAL_EMPTY`를 사용합니다.
필요한 Anthropic 계정은 `ANTHROPIC_WORKSPACE_ID`도 지정할 수 있습니다.

기존 `moderations` 안전 검사와 OpenAI 이미지 생성은 별도 기능이므로
`OPENAI_API_KEY`가 계속 필요합니다. Wan 등 영상 생성 공급사의 키와 모델 선택은
이 모듈에서 변경하지 않습니다.

설정 반영 시 서버를 재시작합니다. 기존 GPT 임시 고정 플래그
`python_backend/work_settings.py`의 `FORCE_DEFAULT_CHAT_MODEL` 기본값은 `False`입니다.

프로토콜 참고

- https://aistudio.google.com/docs/openai
- https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk
