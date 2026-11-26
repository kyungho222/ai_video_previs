# 사용량 등록 복원 · 2026-11-25

- 시나리오·프리비즈·영상의 엔드포인트는 공용 usage_registration 훅으로 등록합니다.
- 기본 OFF. usageLogEnabled=true 체크는 DB 제외 규칙보다 우선하며, 명시적 usageLogOverride=false는 유지합니다.
- 시나리오 DB·chatbot·회원·session 값을 이어받습니다. NULL 회원은 작업 실패 원인이 아닙니다.
- job_type: shortform_scenario / shortform_previs / shortform_video.
- provider/model/modality/billing_unit별로 별도 행 집계, 토큰 NULL은 0.
- request_id는 내부 요청 ID, 공급사 ID는 raw_usage.provider_request_id(s)에 별도 보관합니다.
- raw_usage의 session_id/job_id/request_id/mb_id/job_type은 실제 컬럼과 일치합니다.
- 기존 Wan 해상도×출력 초 비용 공식 복원. 공급사 청구서 대사 금액이 아닌 애플리케이션 추정치입니다.
- 최근 토큰 최적화 유지. 유료 AI 재생성 없이 회귀 테스트 79개 통과.
- 로컬 서버 재시작, health·콘솔 HTTP 200 확인.

## DB 반영

확인된 dev_user.AIPRO_API_USAGE_LOG.session_id만 BIGINT UNSIGNED에서 VARCHAR(128) NULL로 변경했습니다.
기존 숫자값은 보존하며 다른 DB 스키마와 과거 전체 행을 일괄 수정하지 않았습니다.
다른 테넌트 DB에서도 UUID session을 저장하려면 해당 컬럼이 문자열형이어야 합니다.

누락된 실제 프리비즈 03e3d8e4-679d-4dc1-a567-35b5ea01c3b0의 영수증 4건을 모델별 1행, 총 9,167토큰으로 복구했습니다.
session UUID, 조회된 회원 kyunghol, shortform_previs, raw_usage/컬럼 일치를 실제 SELECT로 확인했습니다.
복구 건은 원래 endpoint request_id를 복원할 기록이 없어 job_id를 내부 추적 ID로 사용했습니다.

## 수정 파일

- [usage_registration.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/usage_registration.py)
- [usage_database.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/usage_database.py)
- [stage_usage.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/stage_usage.py)
- [app.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/app.py)
- [shortform_routes.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/shortform_routes.py)
- [video_routes.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_routes.py)
- [video_results.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_results.py)
- [work_settings.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/work_settings.py)
- [usage_history.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/usage_history.py)
- [ai_usage.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/ai_usage.py)
- [store.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/store.py)
- [wan_pricing.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/wan_pricing.py)

## 검증 파일

- [test_restored_usage_database.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_restored_usage_database.py)
- [test_restored_stage_usage.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_restored_stage_usage.py)
