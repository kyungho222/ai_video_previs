#!/usr/bin/env bash
# Copy this file and deploy.compose.yaml next to compose.yaml on the server.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() {
    cat <<'EOF'
사용법:
  bash deploy.sh 1.0.0          이미지 빌드만
  bash deploy.sh 1.0.0 deploy   빌드 후 배포
  bash deploy.sh reload 1.0.0   기존 이미지로 배포 또는 롤백
  bash deploy.sh promote 1.0.0  reload와 동일
  bash deploy.sh push 1.0.0     빌드된 이미지를 레지스트리에 업로드
  bash deploy.sh pull 1.0.0     레지스트리에서 이미지 다운로드
  bash deploy.sh logs [--since 30m]
  bash deploy.sh status
  bash deploy.sh auth-status    Higgsfield 인증 상태 확인
  bash deploy.sh down           컨테이너 중지, 볼륨 유지
EOF
}

compose() { docker compose -f compose.yaml -f deploy.compose.yaml "$@"; }
version_valid() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9][a-zA-Z0-9.-]*)?$ ]] && [ ${#1} -le 100 ]; }
require_version() {
    if ! version_valid "$1"; then echo '버전은 1.0.0 또는 1.0.0-rc1 형식이어야 합니다.' >&2; exit 1; fi
    export VERSION=$1
    export IMAGE_REPOSITORY=${IMAGE_REPOSITORY:-registry.chatbaram.com/ai-pro-videocreate}
    IMAGE="$IMAGE_REPOSITORY:$VERSION"
}
deploy() {
    docker image inspect "$IMAGE" >/dev/null
    compose config --quiet
    # Bind mounts keep host permissions, so prepare writable directories for video.
    compose run --rm --no-deps --user 0 --entrypoint sh video -ec '
        for directory in /app/ai_video/runtime /app/ai_video/runtime/data /app/ai_video/runtime/archive /app/ai_video/runtime/recovered /app/ai_video/runtime/wan /app/ai_video/runtime/wan-inputs; do
            mkdir -p "$directory"
            chown video:video "$directory"
        done
    '
    compose up -d --no-build
    echo "배포 요청 완료: $IMAGE (status 및 auth-status로 확인하세요.)"
}

auth_status() {
    compose exec -T video python - <<'PY'
import json
import sys
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

try:
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:9000/api/jutsu/status", timeout=10) as response:
        auth = json.load(response)
except (URLError, TimeoutError, ValueError):
    sys.exit("컨테이너에서 Higgsfield 인증 상태를 조회할 수 없습니다.")

state = auth["state"]
print(f"Higgsfield [{state}] {auth.get('message', '')}")
sys.exit(0 if state == "connected" else 2)
PY
}

case "${1:-help}" in
    help|-h|--help) usage ;;
    logs) shift; compose logs -f "$@" video ;;
    status) compose ps ;;
    auth-status) auth_status ;;
    down) compose down ;;
    push|pull)
        [ "$#" -eq 2 ] || { usage; exit 1; }
        action=$1
        require_version "$2"
        compose "$action" video ;;
    reload|promote)
        [ "$#" -eq 2 ] || { usage; exit 1; }
        require_version "$2"
        deploy ;;
    *)
        [ "$#" -le 2 ] || { usage; exit 1; }
        require_version "$1"
        [ "${2:-}" = '' ] || [ "$2" = deploy ] || { usage; exit 1; }
        compose config --quiet
        if docker image inspect "$IMAGE" >/dev/null 2>&1; then
            echo "기존 이미지 태그를 새 빌드로 갱신합니다: $IMAGE"
        fi
        compose build video
        echo "빌드 완료: $IMAGE"
        if [ "${2:-}" = deploy ]; then deploy; fi ;;
esac
