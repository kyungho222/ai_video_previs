# 프리비즈 서비스 리팩토링 결과

2026-11-16 기준 로컬 서비스에 반영했습니다.

## 단계별 적용과 검증

| 단계 | 적용 | 검증 |
|---|---|---|
| 1 | RepositoryBundle·저장소 Protocol 도입, app factory에서 운영 저장 구현 주입 가능, 로컬 SQLite·파일 구현 유지 | 저장소 주입·기존 경로·조회 인터페이스, 115개 통과 |
| 2 | 영상 API, 생성 준비, 결과 검증·폴링을 담당 모듈로 분리 | 기존 입력·타임라인·프롬프트·조회 테스트, 115개 통과 |
| 3 | 공용 오류·한도, 미디어 I/O, SQLite 작업·캐릭터 저장, 프롬프트 정책 분리 | 기존 video_common import 호환, 115개 통과 |
| 4 | 음성 언어·보컬·반주 정책 통합, 발성 행동의 무음 프로필 충돌 검사, vocal_mode 추가 | 노래·반주·아카펠라·발화 프로필 회귀 테스트, 117개 통과 |
| 5 | AI HTTP 요청, 원격 도구, 기획, 실행, 렌더 마무리 분리 | 기존 처리 본문 AST 비교, 오류 복구·카메라·API 테스트, 117개 통과 |
| 6 | 기존 자료 초기 이관 1회, 페이지·source별 조회, 생명주기 기록 저장 | 숨기기·삭제·재시작·신규 영상·반복 전체조회 방지, 118개 통과 |
| 7 | app.js 코드를 previs-console01.html에 통합, backend-origin helper import 적용, app.js 파일·정적 등록 제거 | HTML 포함 코드·helper import·정적 경로, 119개 통과 |

최종 통합 회귀 테스트는 125개 모두 통과했습니다. 추가 검증에는 완료된 프리비즈 소스 목록, 실제 3프레임 MP4 업로드·검사, 기존 공급사 작업의 재요청 없는 결과 게시, 원격 조회 재시도와 편집 중복 실행 방지, 이력 저장 오류 중 실행 예약, 발성 모드 충돌이 포함됩니다.

## 로컬 실행 확인

- /health, /previs-console01.html, /api/video/sources, /api/usage-history, helper JS 경로에서 HTTP 200을 확인했습니다.
- /app.js는 HTTP 404입니다. 임시 콘솔 실행 코드는 HTML 안에 있습니다.
- 브라우저에서 인증 연결 상태, 모델 선택, 사용기록 목록, 버튼 클릭 JSON 조회를 확인했습니다.
- JSON의 컬럼 26개와 실제 토큰 숫자를 확인했습니다.
- Python 미정의 전역 참조 검사 및 인라인/보조 JavaScript Node 문법 검사를 통과했습니다.

## 저장 구현 연결

`service_app.create_app(repositories=RepositoryBundle(...))`로 운영 DB 구현을 주입할 수 있습니다. 저장소별 요구 메서드는 `python_backend/repositories.py`에 정의했습니다. 미디어 처리는 인코딩·모델 업로드용 작업 경로를 제공하는 계약을 사용합니다. 운영 DB 어댑터에서 영구 바이트와 작업 경로의 제공·회수를 구현해야 합니다.

로컬 기본 구현은 기존 SQLite와 로컬 영상·에셋 폴더를 사용합니다. 프리비즈 실행 세션은 기존의 프로세스 메모리이며, 프리비즈 사용기록·영상 작업·캐릭터 음성 정보는 SQLite에 저장됩니다.

운영 DB 연결은 사용자 선택에 따라 추후 적용합니다. 공급사 호출은 모의 응답으로 검증했습니다. 새로운 유료 프리비즈·영상 생성과 실제 노래·반주 품질 검증은 이번 실행 범위에 포함하지 않았습니다.

## 코드 검토에서 보완한 경로

- 완료 소스 목록의 제거된 변수 참조를 보완하고 엔드포인트 회귀 테스트를 추가했습니다.
- 구조화된 vocal_mode=none과 실제 노래 행동의 충돌을 검사합니다. 구버전 계획의 while singing 표현도 확인합니다.
- 사용기록 저장 오류를 로그에 남기면서 영상 작업 실행 예약을 진행합니다.

## 수정·추가 파일 전체 경로

- [service_app.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/service_app.py)
- [ai_video/video_higgsfield_jutsu/public/previs-console01.html](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/public/previs-console01.html)
- [ai_video/video_higgsfield_jutsu/python_backend/agent.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/agent.py)
- [ai_video/video_higgsfield_jutsu/python_backend/app.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/app.py)
- [ai_video/video_higgsfield_jutsu/python_backend/audio_policy.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/audio_policy.py)
- [ai_video/video_higgsfield_jutsu/python_backend/camera_reference.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/camera_reference.py)
- [ai_video/video_higgsfield_jutsu/python_backend/character_direction.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/character_direction.py)
- [ai_video/video_higgsfield_jutsu/python_backend/job_repository.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/job_repository.py)
- [ai_video/video_higgsfield_jutsu/python_backend/media_archive.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/media_archive.py)
- [ai_video/video_higgsfield_jutsu/python_backend/model_client.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/model_client.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_execution.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_execution.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_finalization.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_finalization.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_planning.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_planning.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_timeline.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_timeline.py)
- [ai_video/video_higgsfield_jutsu/python_backend/remote_tools.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/remote_tools.py)
- [ai_video/video_higgsfield_jutsu/python_backend/repositories.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/repositories.py)
- [ai_video/video_higgsfield_jutsu/python_backend/spatial_guides.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/spatial_guides.py)
- [ai_video/video_higgsfield_jutsu/python_backend/store.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/store.py)
- [ai_video/video_higgsfield_jutsu/python_backend/usage_history.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/usage_history.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_common.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_common.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_errors.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_errors.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_generation.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_generation.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_media.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_media.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_models.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_models.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_preparation.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_preparation.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_prompt.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_prompt.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_results.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_results.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_routes.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_routes.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_validation.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_validation.py)
- [ai_video/video_higgsfield_jutsu/python_backend/wan.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/wan.py)
- [tests/test_audio_policy.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_audio_policy.py)
- [tests/test_camera_visibility.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_camera_visibility.py)
- [tests/test_character_direction.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_character_direction.py)
- [tests/test_console_bundle.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_console_bundle.py)
- [tests/test_previs_timeline.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_previs_timeline.py)
- [tests/test_prompt_budget.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_prompt_budget.py)
- [tests/test_refactor_workflows.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_refactor_workflows.py)
- [tests/test_storage_contract.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_storage_contract.py)
- [tests/test_usage_history.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_usage_history.py)

## 통합 후 제거한 파일

- C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/public/app.js
