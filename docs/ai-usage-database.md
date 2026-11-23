# AI 사용량 DB 연결 및 테이블 확인 기록

확인일: 2026-11-23. 실제 DB 연결, 컬럼 조회 및 SELECT 성공. 이 점검에서 INSERT는 수행하지 않았다.

## 접속 설정

프로젝트 루트 `.env`에서 관리한다. 비밀번호는 문서나 소스 코드에 복사하지 않는다.

| 환경변수 | 용도 |
| --- | --- |
| `MARIA_DB_HOST` | MariaDB 서버 주소 |
| `MARIA_DB_PORT` | 접속 포트 |
| `MARIA_DB_USER` | 접속 계정 |
| `MARIA_DB_PASSWORD` | 기존 비밀번호 |
| `AI_USAGE_DB_NAME` | 사용량 저장 DB. 확인된 값은 `dev_user` |
| `AI_USAGE_TABLE_NAME` | 확인된 테이블 `AIPRO_API_USAGE_LOG` |

확인된 서버 버전은 MariaDB `10.11.18`이다. `dev_user`에서 `usage_log`라는 이름의 테이블은 확인되지 않았다.
`usage_database.py`가 위 설정으로 외부 DB 저장을 수행한다. 로컬 콘솔 상단의 공통 **사용량 DB 등록** 토글은 기본 OFF이며 새로고침하면 OFF로 돌아간다.
시나리오·프리비즈·영상 생성 요청마다 `usageLogEnabled: true`인 경우에만 저장 대기열에 등록한다.
토글 변경은 이미 실행 중인 요청에 소급 적용되지 않는다. 프리비즈 완료 후 영상 생성 전에 현재 토글을 확인하고 변경할 수 있다.
입력 `db_name`이 정확히 `dev_user` 또는 `aniestkh`인 경우에만 등록한다. 빈 값은 환경변수로 대체하지 않으며, 다른 DB는 `AI_USAGE_ALLOWED_DATABASES`에 있어도 허용하지 않는다.
프리비즈·영상 요청의 등록용 DB명은 이번 요청의 `db_name`을 별도로 보존한다. 이전 시나리오에서 상속한 모델 설정의 DB명이 이번 요청의 빈 값이나 다른 DB명을 덮어써 등록을 허용하지 않는다.
필수 `chatbotid`가 없으면 DB 저장을 건너뛰고 한글 경고를 기록한다. 생성 작업은 계속 진행한다.

사용량 전송 대기열과 전송 완료 식별자는 `ai_video/runtime/data/usage-db-outbox.sqlite3`에 보관한다.
서버는 5초 간격으로 대기열을 확인하고 별도 스레드에서 INSERT한다. 실패 항목은 다음 점검에 재시도한다.
완료 기록과 DB의 `_tracking.usage_id`를 확인하여 동일 단계·공급사·모델·과금 단위의 중복 INSERT를 방지한다. DB 테이블은 자동으로 변경하지 않는다.
`mb_id`는 `AI_USAGE_MB_ID` 설정을 사용하며 기본값은 `shortform`이다. 실제 로그인 사용자 연동은 별도 작업이다.
시간은 UTC로 저장한다.

## 실제 컬럼 구조

| 컬럼 | 타입 | NULL | 비고 |
| --- | --- | --- | --- |
| id | BIGINT | 불가 | AUTO_INCREMENT. INSERT에서 제외 |
| message_id | BIGINT | 허용 | 앞단 메시지 ID |
| chatbot_id | VARCHAR(36) | 불가 | 앞단 `chatbotid` 매핑 |
| session_id | BIGINT UNSIGNED | 허용 | 현재 서비스의 UUID `sessionId`와 타입 불일치 |
| job_id | VARCHAR(128) | 허용 | 프리비즈 또는 영상 작업 ID |
| request_id | VARCHAR(128) | 허용 | 내부 요청 ID |
| mb_id | VARCHAR(50) | 허용 | 사용자 식별자 |
| job_type | VARCHAR(32) | 불가 | 기본값 `chat` |
| provider | VARCHAR(32) | 불가 | 실제 제공자 |
| model | VARCHAR(64) | 불가 | 실제 호출 모델 |
| modality | VARCHAR(16) | 불가 | 표기 정규화 필요 |
| billing_unit | VARCHAR(16) | 불가 | token, second 등 |
| input_tokens | BIGINT | 허용 | 미제공 시 NULL |
| output_tokens | BIGINT | 허용 | 미제공 시 NULL |
| cached_tokens | BIGINT | 허용 | 미제공 시 NULL |
| total_tokens | BIGINT | 허용 | 미제공 시 NULL |
| quantity | INT | 허용 | 소수 사용량은 그대로 저장 불가 |
| duration_seconds | DECIMAL(10,3) | 허용 | 영상 길이 |
| credits_charged | DECIMAL(20,8) | 불가 | 기본값 0.00000000 |
| cost_usd | DECIMAL(20,8) | 허용 | 미제공 시 NULL |
| call_status | VARCHAR(16) | 불가 | 기본값 success. 현재 상태값 정규화 필요 |
| error_code | VARCHAR(64) | 허용 | 수집 코드 길이 검증 필요 |
| latency_ms | INT | 허용 | 호출 소요 시간 |
| raw_usage | LONGTEXT | 허용 | JSON 직렬화하여 저장 |
| created_at | DATETIME(3) | 불가 | 기본값 CURRENT_TIMESTAMP(3). 저장 시간대 정의 필요 |

## 연동 시 유지할 규칙

- 단계 종료 시 `scenario_generation`, `previs_chat`, `video_generation`별로 사용량을 집계한다. 동일 단계·job·provider·model·modality·billing_unit인 호출만 합산한다.
- 단계마다 모델 하나·과금 단위 하나만 쓰면 총 3행이다. fallback 또는 영상 준비·검증 모델처럼 여러 모델을 사용하면 모델별 행이 추가된다. 모델 간 토큰이나 초 단위·토큰 단위의 quantity를 섞지 않는다.
- 개별 호출의 `ai_usage[]`는 기존 응답과 로컬 기록에 유지하고, DB 행의 `raw_usage.calls`에도 보존한다. 누락된 계측값은 0으로 만들지 않으며 일부만 알려진 합계는 `raw_usage.partial_fields`에 표시한다.
- `raw_usage.stage_status`는 전체 단계 결과이고 `call_status`는 해당 모델 그룹의 결과다. 성공한 단계에서도 실패한 fallback 원본 모델 그룹은 실패로 기록된다.
- 기능 기본값은 OFF이다. 요청의 `usageLogEnabled`가 정확히 true이고 허용 DB가 명시된 경우에만 등록하며, DB 저장 실패는 생성 작업의 결과에 영향을 주지 않는다.
- 현재 사용량 JSON의 UUID `id`를 테이블의 정수 `id`에 넣지 않는다.
- UUID `sessionId`는 `session_id`에 NULL로 저장하고 `raw_usage._tracking.session_id`에 원문을 보존한다.
- 프리비즈 사용량은 해당 job ID를, 영상 사용량은 영상 job ID와 `_tracking.source_job_id`에 원본 프리비즈 ID를 기록한다.
- 시나리오 생성은 독립 UUID `job_id`를 발급해 시나리오 저장본과 응답에도 포함한다. 비동기 프리비즈 job과는 별도이며, 같은 sessionId로 연결된다.
- `provider_request_id` 전용 컬럼은 없다. 필요하면 `raw_usage`에 별도 필드로 보존한다.
- DB 자동 증가 ID와 별도로 `_tracking.usage_id`가 단계 내 모델별 집계를 식별한다. 개별 호출 ID는 `raw_usage.calls[].id`에 보존한다.
- 사용량 정보 누락 또는 DB 저장 실패가 프리비즈·영상 생성을 실패시키지 않도록 분리한다.
- 비밀번호 및 DB 인증 정보를 사용량 JSON이나 서버 로그에 기록하지 않는다.
