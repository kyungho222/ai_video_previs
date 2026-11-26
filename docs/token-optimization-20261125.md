# 2026-11-25 숏폼 토큰 최적화 실행 결과

## 실행 범위

복원한 원본에서 단계별 변경을 적용했다. 텍스트 기본 모델은 Gemma `google/gemma-4-26B-A4B-it`, 호출 실패 fallback은 `gpt-6-luna`, 영상은 Wan `wan3.0-video`, 10초·16:9·480P이다.
시나리오는 1회 생성해 재사용했고 프리비즈는 초기 1회와 보정 1회, Wan 생성은 1회 수행했다. 완성 영상의 실제 AI 검증은 1회이다. 추가 유료 재생성은 하지 않았다.

## 사용량

| 단계 | 정상 완료 경로의 계측 토큰 |
| --- | ---: |
| 시나리오 | 2,445 |
| 보정된 프리비즈 | 6,460 |
| 영상 준비·이미지 생성·최종 검증 | 30,926 |
| 새 테스트 에셋 시트 준비 | 304 |
| 합계 | **40,135** |

초기 프리비즈 5,388토큰과 최초 매핑 실패 9,951토큰을 포함하면 **계측된 실험 누적은 55,474토큰**이다.
기존 코드가 영상 요청 안전검사의 usage를 저장하지 않아 두 번의 요청에서 텍스트 검사 총 4건이 누락됐다. 정상 완료 경로에도 그중 2건이 포함된다. 이를 0으로 간주하지 않는다. 과거 usage는 정확히 복원할 수 없으므로 모든 실패·검사를 포함한 완전한 5만 토큰 목표 달성은 확정하지 않는다.
이 누락은 최종 코드에서 안전검사 receipt 수집과 `metadata.ai_usage` 병합으로 보완했고 오프라인 API 테스트로 확인했다. 모델별 receipt ID로 중복 제거하고 입력·출력·합계를 모델별로 유지한다.

## 최적화 내용

- 같은 예시의 공통 영상 프롬프트: 11,003자 → 2,547자. 문자 수 비교이며 전체 토큰 절감률을 뜻하지 않는다.
- 확정된 내부 프리비즈의 설계·동선·카메라 의도를 재사용해 별도 카메라 분석 AI 호출을 생략했다. 실제 렌더 카메라 좌표를 다시 측정한 것은 아니다.
- 프롬프트 미화 AI 호출을 기본 생략하고 Python이 사용자 요청·원래 장면·참조 계약을 조립한다.
- 시작 이미지 한 장을 한 번 생성해 first_frame으로만 전달한다. 종료 이미지는 생성·전달하지 않는다.
- 이미지 생성에는 첫 장면의 상태를 보내고 전체 행동·음성·미래 타임라인 반복 전달을 줄인다.
- 매핑 응답은 명시적 `yxyx_1000` 정수 계약을 사용하고 Python이 표준 `xyxy_0_1`로 변환한다. 단위를 추측해 바꾸지 않는다.
- 입력 검증은 문제 목록과 매핑을 반환하며 전체 프롬프트를 다시 작성하지 않는다.
- 최종 영상 검증은 결과 4장·프리비즈 2장·원본 에셋 3장으로 제한했다. 추가 움직임 표본이 0장인 경우도 처리한다.
- fallback 모델의 reasoning만으로 출력 한도가 소진된 실측 문제를 반영해 Luna에 최소 4,096 출력 토큰 여유를 준다. 여유 한도는 실제 사용량과 다르다.

실제 생성에 전달한 최종 프롬프트는 **9,491자**이다. 8,000자는 목표값으로 두고 필수 동선·대사를 보존해 초과를 허용했다.

## 검토 결과

- 파일: H.264, 854×480, 30fps, 영상 10초(오디오 포함 컨테이너 약 10.031초), AAC 44.1kHz 스테레오.
- 최종 표본 검증: 인물 수·회색 상의/검정 바지·테이블·흰 컵·보이는 신체 연결은 통과. 컵을 든 상태와 테이블에 내려놓은 상태가 확인됐다.
- **잔여 문제:** 시작의 어두운 배경이 밝은 실내로 바뀌어 무컷·동일 공간 조건을 완전히 만족하지 못했다. 정확한 카메라 경로와 프레임 사이의 동작 자연스러움은 표본만으로 확정할 수 없다.
- 음향은 음악·효과음 없이 한국어 인사 목소리만 요청했다. 오디오 트랙과 신호는 확인했으나 청취 기능을 사용할 수 없어 실제 음악 유무·한국어 발화는 확정하지 않았다. `audio-review.wav`를 남겼다.
- 첫 프리비즈의 인물/테이블 중첩과 인물 root 부유는 보정 요청에서 위치·발/root 의미를 명확히 해 줄였다. 완전 무결점으로 판정하지 않는다.

## 최종 모듈화와 검증 범위

- `video_policy.py`: 모든 영상 어댑터가 재사용할 공통 지시.
- `previs_reference.py`: 저장된 장면 설계 재사용과 첫 프레임 전용 장면 정보 선택.
- `mapping_coordinates.py`: 명시된 좌표 계약의 순서·단위 변환.
- 기존 공개 import는 유지하고 메인 준비 경로가 이 기능을 호출하도록 했다.

최종 코드 검토에서 사용자 에셋 지정 요청 누락, 안전검사 usage의 최상위 집계 누락, 첫 장소 설명이 후속 장소에 강제되는 문제를 보완했다. 이 마지막 보완과 모듈 분리는 **오프라인 회귀 테스트 9개 통과 및 문법 검사**로 검증했으며, 사용자 요청의 반복 한도에 따라 유료 전체 파이프라인을 다시 실행하지 않았다. 따라서 위 40,135토큰은 완료된 관측 실행의 값이며, 마지막 코드의 새 실측값이라고 주장하지 않는다.

## 저장 결과

`ai_video/runtime/optimization-tests/2026-11-25-token50k/`에 영상·프리비즈·에셋·실행 응답·사용량·검증 결과를 보관한다. 원본 실행 자료와 실패/보정 자료를 함께 남긴다.

## 수정 파일 전체 경로

- [ai_video/model_gateway/model_mapping.json](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/model_gateway/model_mapping.json)
- [ai_video/model_gateway/routing.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/model_gateway/routing.py)
- [ai_video/video_higgsfield_jutsu/public/previs-console.js](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/public/previs-console.js)
- [ai_video/video_higgsfield_jutsu/public/wan-upload.html](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/public/wan-upload.html)
- [ai_video/video_higgsfield_jutsu/public/wan-upload.js](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/public/wan-upload.js)
- [ai_video/video_higgsfield_jutsu/python_backend/app.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/app.py)
- [ai_video/video_higgsfield_jutsu/python_backend/character_direction.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/character_direction.py)
- [ai_video/video_higgsfield_jutsu/python_backend/mapping_coordinates.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/mapping_coordinates.py)
- [ai_video/video_higgsfield_jutsu/python_backend/model_client.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/model_client.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_reference.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_reference.py)
- [ai_video/video_higgsfield_jutsu/python_backend/previs_timeline.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/previs_timeline.py)
- [ai_video/video_higgsfield_jutsu/python_backend/prompt_filter.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/prompt_filter.py)
- [ai_video/video_higgsfield_jutsu/python_backend/scene_pipeline.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/scene_pipeline.py)
- [ai_video/video_higgsfield_jutsu/python_backend/shortform_scenario.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/shortform_scenario.py)
- [ai_video/video_higgsfield_jutsu/python_backend/spatial_guides.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/spatial_guides.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_policy.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_policy.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_preparation.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_preparation.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_prompt.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_prompt.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_routes.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_routes.py)
- [ai_video/video_higgsfield_jutsu/python_backend/video_validation.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/ai_video/video_higgsfield_jutsu/python_backend/video_validation.py)
- [tests/test_token_optimization.py](C:/Users/COM-BCAEO/Documents/mymymymy/company/ai_video_service_previs/tests/test_token_optimization.py)
