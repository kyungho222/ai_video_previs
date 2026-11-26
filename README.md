# 프리비즈 · Wan 독립 서비스

콘솔에서 숏폼 시나리오, 3D 프리비즈, Wan 영상을 생성하고 사용기록을 확인할 수 있습니다.
시나리오를 생성하면 제목과 프롬프트가 포함된 3~4개 씬이 채워집니다. 프리비즈 탭에서
길이와 화면 비율을 선택하고 요청을 보내면 프리비즈가 생성됩니다. 생성된 프리비즈나
업로드한 MP4와 에셋을 사용해 최종 영상을 만들 수 있습니다.

같은 세션의 후속 요청은 현재 장면 수정으로 처리합니다. 요청마다 v1, v2 순서로
사용자 지시, 분리된 장면·카메라 계획, 생성 코드와 상태를 서버 메모리의 작업 저장소에 기록합니다.
실패한 버전도 번호를 유지하며 다음 수정은 마지막으로 MP4와 코드가 저장된 성공 버전을 기준으로 합니다.
각 버전은 별도 원격 프로젝트에서 구성하여 앞선 버전의 장면을 보존합니다.
초기화 또는 명시적인 새 장면 생성은 새 장면의 v1로 시작합니다.
화면의 버전 기록에서 요청별 상태와 계획·코드를 확인할 수 있습니다.
생성한 프리비즈와 영상은 완료 후 5일간 보관합니다. 진행 중인 프리비즈 작업과 세션의 버전 상태는
서버 재시작 시 초기화됩니다. 보관된 결과와 사용기록은 디스크에 저장됩니다.

프리비즈 생성은 **선택 모델의 JSON 장면 설계 → Python 검증 → Python 장면 구성 → 렌더링**으로 처리합니다.
모델은 인물·사물의 고유 ID, 씬 순서, 행동 시간표, 카메라 구도와 시선 대상을 결정합니다.
Python은 좌표·키프레임·관절 연결, 실제 대상 경계를 이용한 카메라 거리 계산, 프로젝트 생성과 ID 유지,
원격 Blender 실행, MP4 저장·반환을 담당합니다. 모델이 반환한 Python 코드나 도구 호출은 실행하지 않습니다.

설계 JSON은 `scene_contract.py`, 실행 코드는 `scene_compiler.py`와 `resources/scene_runtime.py`,
원격 실행 순서는 `scene_pipeline.py`에서 관리합니다. 작업의 `sceneDesign`에 검증된 원본 JSON을,
`plan`에는 기존 영상 생성과 시나리오별 스냅샷에 호환되는 타임라인을 저장합니다.
후속 수정에서는 이전 설계와 요청을 같은 모델에 전달하고, 기존 버전을 보존하도록 새 버전용 프로젝트를 만듭니다.
동일 작업의 보정에는 기존 프로젝트를 재사용하며, 프로젝트 생성 여부가 불명확하면 목록을 확인하고 중복 생성을 중단합니다.

시나리오·프리비즈 경로는 최초 선택 모델을 유지합니다. API 장애 또는 JSON 오류로 GPT에 자동 전환하지 않습니다.
설계 형식·ID·좌표·시간표 검증에 실패하면 같은 모델에 최대 3회 생성·수정을 요청합니다.
확인된 카메라 구도 데이터 오류는 같은 프로젝트에서 같은 모델로 한 차례 설계를 보정합니다.
인증·요청량 제한·원격 Python 구현 오류는 임의의 모델 코드로 우회하지 않고 원인을 기록합니다.
입력 안전 검사는 유지하며, 별도 OpenAI moderation 서비스는 채팅 모델 전환에 해당하지 않습니다.
Gemma의 JSON 객체 출력에는 작업 지시와 출력 규격을 하나의 시스템 메시지로 전달합니다.
입력 검사에서 스키마 자체를 반환하는 등 형식이 잘못되면 같은 모델에 한 번 보정 요청하고,
계속 잘못되면 HTTP 503으로 중단합니다. 잘못된 검사 응답을 안전 판정으로 간주하지 않습니다.

현재 실행기는 사람·네발 동물·새의 프록시와 상자·구·원통 사물, 이동·걷기·달리기·손 흔들기·손 들기·
가리키기·뻗기·유지·손 휘두르기·회전·앉기·웅크리기·점프·넘어지기·눕기·비행의 블로킹을 제공합니다.
카메라는 고정·추적·궤도 이동·접근·팬과 와이드·전신·미디엄·클로즈 구도를 지원합니다.
정교한 표정·손가락·특정 사물의 기능과 최종 음성은 이 프록시 실행기의 재현 범위가 아니며,
원래 연출 의도는 장면 설명과 프롬프트에 유지해 최종 영상 모델에 전달합니다. 새로운 세부 동작은 Python 실행기에 추가해야 합니다.

제작 시작 시 기존 Higgsfield 워크플로 지침을 확인하며, 서버 로그는 `[previs-design]`,
`[previs-project]`, `[previs-scene]`, `[previs-finalize]`로 설계·실행·오류를 간소하게 기록합니다.
이전 자유 코드 생성 모듈은 저장된 코드 조회와 호환 검증을 위해 남겨두며 새 작업의 기본 경로에서는 사용하지 않습니다.

채팅 모델 기본값은 `gpt-6.1-terra`입니다. `PREVIS_DEFAULT_CHAT_MODEL`로 기본값을,
`PREVIS_CHAT_MODELS`에 쉼표로 구분한 모델 ID로 선택 목록을 설정할 수 있습니다.
모델 ID의 API 지원 여부와 계정 권한은 별도로 확인해야 합니다.

서버에서 `.env`를 준비하고 OPENAI_API_KEY, Alibaba API 키, 실제 공개 URL과
Higgsfield OAuth 콜백 설정을 확인하세요. 기존 서비스와 동일한 프로젝트명과
볼륨을 사용해야 이전 인증과 생성 결과에 접근할 수 있습니다.

```bash
docker compose config --quiet
bash deploy.sh 1.0.0 deploy
bash deploy.sh status
bash deploy.sh logs
curl http://127.0.0.1:9000/health
```

실제 공급사 인증과 영상 생성은 별도 확인이 필요합니다. /health는 프로세스 상태만 확인합니다.

### Docker 레지스트리

전체 서비스 이미지의 기본 저장소는 `registry.chatbaram.com/ai-pro-videocreate`입니다.
`IMAGE_REPOSITORY` 환경변수로 다른 저장소를 지정할 수 있습니다.

```bash
# 빌드 PC: 로그인 후 빌드 및 업로드
docker login registry.chatbaram.com
bash deploy.sh 1.0.2
bash deploy.sh push 1.0.2

# 운영 서버: 다운로드 후 기존 볼륨을 유지하여 실행
bash deploy.sh pull 1.0.2
bash deploy.sh reload 1.0.2
bash deploy.sh status
bash deploy.sh auth-status
```

이미지는 API·콘솔·인증 모듈·사용량 DB 연결·영상 처리 도구와 Playwright/Chromium을 포함합니다.
`.env`, `env.txt`, 인증 토큰·브라우저 프로필·생성 결과는 포함하지 않습니다.
운영 서버의 `.env`와 기존 `ai_video/runtime`, `previs-auth` 볼륨을 사용합니다.
컨테이너의 브라우저는 기본 headless이며, 초기 로그인은 준비된 영구 프로필이 필요합니다.
이전 로컬 이미지 이름으로 롤백할 때는 `IMAGE_REPOSITORY=ai-pro-videocreate bash deploy.sh reload <버전>`을 사용합니다.

### Windows 로컬 서버 재시작

프로젝트 루트에서 PowerShell을 열고 다음 명령을 실행합니다.

```powershell
.\restart-local.ps1
```

기존 로컬 서버를 종료하고 백그라운드로 다시 시작한 뒤 서버와 Higgsfield 연결 상태를 표시합니다.
재시작하면 진행 중인 작업이 중단될 수 있습니다. 스크립트 실행 정책으로 차단될 경우에는 다음 명령을 사용합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\restart-local.ps1
```

`restart-local.ps1`은 로컬 환경 전용 파일로 `.gitignore`에 포함되어 있어 Git에서 내려받지 않습니다.

### Higgsfield 브라우저 자동 승인

ID·PW를 읽어 입력하는 자동 로그인은 제거했습니다. `.env`의 `id`, `password`는 인증 코드에서 사용하지 않습니다.
기본 동작은 저장된 OAuth 토큰 복구·갱신이며, 재승인이 필요하면 콘솔의 연결 버튼으로 일반 브라우저에서 승인합니다.
서버가 Chromium을 자동으로 여는 기능은 기본 OFF입니다. 기존 로그인 세션으로 승인 화면만 자동 처리해야 하는 경우에만
`JUTSU_AUTH_BROWSER_AUTO_APPROVE=1`과 전용 프로필을 명시합니다. Google ID·PW 자동 입력은 지원하지 않습니다.

인증 기능은 `ai_video/jutsu_auth` 패키지에서 관리합니다. `manager.py`는 연결 점검·자동 복구·
브라우저 승인·준비 대기·종료를 담당하고, `routes.py`는 기존 `/api/jutsu/*` API를 제공합니다.
메인 서비스는 관리자 초기화·시작·종료 및 라우터 등록만 수행합니다. 프리비즈와 영상 참조 준비는
공통 인증 준비 함수를 호출하며, 준비 대기는 `JUTSU_AUTH_READY_TIMEOUT_SECONDS`(기본 300초,
최대 600초)로 제한합니다. 인증 복구 때문에 이미 제출한 장면 변경 요청을 재전송하지 않습니다.
새 OAuth 흐름은 이전 흐름의 600초 재시도 제한 없이 브라우저 승인 대상이 됩니다.
OAuth SQLite와 브라우저 프로필의 기존 저장 위치는 유지됩니다.

서버에 `JUTSU_AUTH_BROWSER_AUTO_APPROVE=1`과 `JUTSU_AUTH_BROWSER_PROFILE`을 지정하면, 저장된 인증 복구에 실패해 새 권한 승인이
필요할 때 서버가 전용 Chromium을 띄웁니다. 설정된 콜백과 앱을 확인한 뒤 승인 화면에서
**계속**을 누릅니다. 전용 프로필에 로그인 세션이 없다면 열린 브라우저에서 Higgsfield에
로그인해야 합니다. 이후 실행에서도 같은 프로필을 사용합니다.

Windows 로컬 콘솔에서 활성화하는 예시입니다.

```powershell
python -m pip install playwright
python -m playwright install chromium
$env:JUTSU_AUTH_BROWSER_AUTO_APPROVE = '1'
$env:JUTSU_AUTH_BROWSER_PROFILE = Join-Path (Get-Location).Path 'ai_video/runtime/data/jutsu-browser-profile'
.\start-local.ps1
```

서버에서 활성화하려면 서비스 실행 환경에 Playwright 및 Chromium을 설치하고, 서비스
사용자가 쓸 수 있는 전용 영구 프로필 경로를 지정합니다. 브라우저 창을 띄울 수 없는 환경은
로그인이 준비된 프로필과 `JUTSU_AUTH_BROWSER_HEADLESS=1`이 필요합니다.

## 데이터 저장과 전체 초기화

프리비즈 작업 조회 `GET /api/jobs/{jobId}`는 기본으로 완료 대기 모드입니다.
진행 중에는 최대 25초 동안 기다리고 미완료 시 본문 없는 `204`를 반환합니다.
클라이언트는 `204`를 JSON으로 파싱하지 않고 약 1초 후 재조회합니다.
완료 시 `state=completed`와 최종 `complete` 이벤트·전체 토큰 사용량을 반환하며,
실패 시 `state=failed`와 오류를 반환합니다. 내부 진행 로그와 도구 응답은 서버에 유지합니다.
기존 진행 조회가 필요한 진단용 클라이언트는 `?completeOnly=false`를 명시합니다.
콘솔과 API 클라이언트 스크립트를 백엔드와 함께 반영해야 합니다.

완성된 프리비즈·생성 영상·업로드 원본은 영구 보관합니다. 새 파일과 영상 작업의
`expiresAt`은 `null`이며 자동 정리는 완성된 파일을 삭제하지 않습니다.
오래된 미완료 `.part` 파일의 정리는 별도로 유지합니다.

기존 자료 전환 API `POST /api/usage-history/retention/permanent`는 유지하며,
콘솔 History의 일회성 전환 버튼과 호출 스크립트는 제거했습니다.
API는 페이지·숨김 여부와 무관하게 저장소 전체의 남아 있는 파일 인덱스와 완료 영상
작업의 만료값을 영구 보관으로 전환합니다. 반복 실행해도 안전하며 생성·삭제 요청은 하지 않습니다.
파일이 남아 있다면 만료된 자료도 다시 조회할 수 있고, 이미 삭제된 파일은 복원하지 않습니다.
SQLite 운영 저장소와 로컬 JSON 저장소가 같은 API를 지원합니다.

생성 관련 영구 데이터는 `ai_video/runtime` 아래에 저장합니다. Docker에서는 같은 구조를
`/app/ai_video/runtime`에 서버 프로젝트의 `./ai_video/runtime`을 직접 연결합니다.
운영 프로젝트가 `/home/chat_bot/AI_Pro_VideoCreate`라면 실제 저장 위치는
`/home/chat_bot/AI_Pro_VideoCreate/ai_video/runtime`입니다.
콘솔 History도 동일한 연결을 통해 `archive/usage-history.sqlite3`에서 기록을 읽고,
`archive`, `recovered`, `wan`의 영상과 `data`의 작업 정보를 조회합니다.

| 하위 폴더 | 저장 내용 | 서버 폴더 |
| --- | --- | --- |
| `data` | 영상 작업·캐릭터 등록 DB `wan-jobs.sqlite3`, 파일 등록 DB `media-archive.sqlite3`, 에셋–객체 연결 DB `asset-object-bindings.sqlite3`, 업로드 에셋 `wan-assets/` | `ai_video/runtime/data` |
| `archive` | 프리비즈 보관 파일과 콘솔 History DB `usage-history.sqlite3` | `ai_video/runtime/archive` |
| `recovered` | 복구된 프리비즈 결과와 메타데이터 | `ai_video/runtime/recovered` |
| `wan` | 최종 영상, 씬 스냅샷, 가이드 이미지 | `ai_video/runtime/wan` |
| `wan-inputs` | 업로드한 프리비즈 MP4와 메타데이터 | `ai_video/runtime/wan-inputs` |

로컬에서 작업 내역, History, 생성 결과를 전부 초기화하려면 서버를 중지한 뒤 위 다섯 하위 폴더를
삭제합니다. 재시작하거나 새 생성 작업을 수행하면 필요한 폴더가 다시 만들어집니다.
Docker에서도 서비스 중지 후 서버의 위 다섯 폴더를 초기화합니다. 인증 유지가 필요하면
`previs-auth` 볼륨과 로컬의 `ai_video/jutsu_auth/data`를 보존합니다.

이전 컨테이너의 `/app/ai_video/video_higgsfield_jutsu/data`에 남은 작업 데이터를 보존하려면
컨테이너를 교체하기 전에 백업해 `ai_video/runtime/data`로 옮깁니다.
기존 Docker 볼륨을 사용하던 운영 서버는 서비스를 중지하고 다섯 볼륨의 내용을
위 표의 대응 폴더로 복사·대조한 뒤 새 Compose 설정으로 컨테이너를 재생성해야 합니다.
MP4뿐 아니라 DB와 JSON 메타데이터도 함께 복사해야 기존 History를 유지할 수 있습니다.
이관 확인 전에는 기존 볼륨을 삭제하지 않습니다.
`deploy.sh`는 배포 전에 마운트된 폴더의 소유자를 서비스 사용자로 설정합니다.
[배포 및 로그 명령어](배포_및_로그_명령어.md)

영상모델 관련 코드는 두 파일로 통합했습니다.

- `python_backend/video_common.py`: 공통 프롬프트 정책, 프리비즈·에셋 등록/보관, 미디어 검사/다운로드,
  공개 네트워크 검증, SQLite 작업 저장·중복 방지·실행권 관리.
- `python_backend/wan.py`: Alibaba/Wan 인증·업로드·요청 형식·작업 조회 및 `/api/wan/*` 연결.

`wan_assets.py`, `wan_previs.py`, `wan_prompt.py`, `wan_routes.py`, `wan_store.py`는 통합 후 제거했고,
미사용 구간 추출 코드 `wan_segments.py`도 제거했습니다. `video_generate/prompt_policy.py`의 정책도
공용 파일로 이동했습니다. 기존 Wan API 주소와 SQLite `wan_jobs` 테이블을 유지합니다.
다른 모델은 VideoJobs에 별도 DB 경로·표시 이름을 지정하고, 공통 에셋 및 프리비즈 저장소를 재사용할 수 있습니다.
서버 업데이트 시 새 파일뿐 아니라 제거된 파일과 `app.py`, `media_archive.py`의 변경도 반영한 뒤 재빌드하세요.

자동 수정 흐름: 사전 검증 → AI 수정 → 필요 시 부분 재구성·보완 → 재검증 → 씬 구성·렌더링 → MP4 조회.
확정된 원격 Python 예외·실행시간 초과도 오류·stderr·현재 코드와 함께 AI에 전달합니다.
변경 코드에는 새 operationId를 사용하며, 같은 실패 operationId의 재실행을 차단합니다.
통신 결과가 불확실하거나 인증이 실패한 경우에는 장면 변경을 무작정 재전송하지 않습니다.
전체 메인 처리 단계는 24회로 제한하며, 완료되지 않은 작업을 성공 처리하지 않습니다.
수정 이력은 작업의 repairHistory에 보관합니다. 인사처럼 일부 관절만 필요한 동작은 AI가
previs_register_motion(..., required_markers=['R_Hand'])처럼 실제 요청에 필요한 관절을 지정합니다.
동작을 채우는 코드는 AI가 작성하며 검증기가 임의의 안무를 삽입하지 않습니다.


## 공간 가이드와 동작 가이드

콘솔과 업로드 화면에서 **첫 씬 기준 · 후속 씬 연속 연결 (테스트)** 방식을 선택할 수 있습니다.
실제 비교 시험이 첫 기준 이미지 검증에서 중단되어 기본값은 기존 시작·종료 이미지 방식으로 유지합니다.
API의 `referenceMode=text_continuity`는 첫 이미지를 시작 상태의 기준으로 사용하고,
후속 이미지·타임라인은 행동·대사·사건·구도 의도를 참고합니다. 다음 씬의 절대 위치나 자세로
초기화하지 않고 직전 위치·속도·접촉 상태와 카메라 흐름에서 이어갑니다.
명시적인 컷·장소 변경·시간 점프와 필수 사건 시점은 유지합니다.
이미지는 모두 참조 이미지로 보내며 종료 이미지를 강제 종료 프레임으로 전달하지 않습니다.
첫 가이드의 검증에 실패하면 후속 가이드로 대체하지 않고 해당 생성 준비를 중단합니다.
기존 `text_endpoints`와 API의 `auto` 기본 동작, 이미 저장된 요청의 재시도 설정은 유지합니다.

Wan 생성의 기본값은 `referenceMode=spatial`입니다. 프리비즈의 소수 프레임과 시간 흐름을 분석하고, AI가 주요 씬을 선택하여 등록 에셋으로 다시 그린 공간 가이드를 검증하고 참조 이미지로 전달합니다. 기본 방식에서 MP4는 Wan에 전달하지 않습니다. `spatial_motion`을 선택하면 공간 이미지와 원본 동작 MP4를 함께 전달합니다. 기존 `video`(MP4+에셋), `images`(에셋+텍스트) 방식도 유지합니다.

`spatialInput`은 `references`, `first_frame`, `first_last`입니다. 시작/종료 이미지는 다른 참조 이미지·영상과 혼합하지 않습니다. 참조 이미지 방식에서는 에셋과 공간 이미지 합계가 공급사 한도인 10장 이하여야 합니다. `spatialGuideCount`의 기본값은 `auto`입니다. 원본 에셋과 가이드 합계 10장 안에서 AI가 필요한 주요 씬을 선택합니다(에셋 3장 → 가이드 최대 7장). 숫자 1~9를 지정하면 생성 수의 상한으로 사용합니다. 비슷한 구도로 남은 자리를 채우지 않습니다. 시작 이미지는 1장, 시작·종료는 2장입니다.

생성한 Blender 장면은 객체 ID·계층·대상 루트와 카메라의 프레임별 좌표를 JSON 아티팩트로 내보냅니다. 해당 정보가 없거나 업로드 MP4만 있으면 명시적으로 2D 영상 분석을 사용합니다. 이는 관절 추적/정확한 3D 복원을 보장하지 않습니다.

공간 가이드 생성에는 `OPENAI_API_KEY`가 필요합니다. 이미지 모델은 `SPATIAL_GUIDE_IMAGE_MODEL`(기본 `gpt-image-2`), 분석 모델은 `VIDEO_PROMPT_MODEL` 또는 기본 채팅 모델을 사용합니다. 이미지 생성·분석 요금이 영상 생성 요금에 추가됩니다. 가이드 검증 실패 시 발견한 문제를 이미지 모델에 전달해 한 번 수정합니다(이미지당 최대 2회 생성). 응답 유실로 접수 여부를 알 수 없는 이미지 요청은 재전송하지 않습니다. 수정 후에도 구도·에셋 검증에 실패하면 Wan 유료 호출 전에 중단하며 원본 프리비즈로 자동 대체하지 않습니다.

프롬프트만으로 이미지 모델의 구도·크기 보존을 보장할 수 없습니다. 실제 매/토끼 예제에서 외형 교체는 되었으나 위치·크기가 변하여 검증이 거절했습니다. 이 구조의 영상 품질 향상은 동일 장면의 A/B 생성으로 별도 평가해야 합니다. 가이드 이미지와 진행 상태는 작업 카드 및 `/api/wan/jobs/{id}`에서 확인합니다. 미리보기는 `/api/wan/jobs/{id}/guides/{index}`입니다. 파생 가이드 파일에도 5일 보관 정책을 적용합니다.


### AI 주요 씬 선정

새 요청은 `spatialGuideSelection=key_scenes_v1`을 저장합니다. 씬/장소 전환, 카메라 구도·샷 크기 변화, 캐릭터 첫 등장·재등장, 상대 배치 변화, 최종 구도를 기준으로 AI가 선택합니다. 기본 공간 참조 이미지 모드에서는 시작·최종 구도를 우선 확보하고 추가 씬을 중요도순으로 배정합니다. 한 자리만 남으면 최종 구도를 선택합니다. 기존 접수 작업의 선택 정책과 요청 ID는 유지합니다.

영상 전체를 시간 간격과 시각 변화로 조사하고 최대 64개의 저해상도 후보 이미지를 AI에 보여줍니다. 긴 영상도 받지만, 유한한 후보 분석이라 아주 짧은 이벤트의 완전한 검출은 보장하지 않습니다. 선택한 실제 프레임 번호·시각·사유는 가이드 manifest와 작업 metadata에 저장하여 재개 시 동일하게 사용합니다. AI 분석 실패를 균등 간격 선택으로 몰래 대체하지 않습니다. 로그 `[spatial-scenes]` 및 작업 카드에서 선정 사유와 예산 내 선택 수를 확인할 수 있습니다. 이미지 수가 늘면 생성 비용과 대기 시간이 늘어납니다.
