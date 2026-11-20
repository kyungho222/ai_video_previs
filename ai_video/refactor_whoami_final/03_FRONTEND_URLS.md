# 프런트와 관리 API URL

| 구분 | 주소 |
| --- | --- |
| 프런트 관리 화면 | 프런트 사이트 주소 + `/home/chatty.kr/admin/work/account/model_config.htm` |
| 관리 API | `http://dev.chatbaram.com:9010/admin/model-registry` |
| 모델 목록 조회 API | `http://dev.chatbaram.com:9010/admin/model-registry/list` |

API 주소는 현재 개발 프런트 소스의 값입니다. 다른 환경은 `account/model/model.php`의 `$url1`을 해당 백엔드 주소로 맞춥니다. 프런트 사이트의 도메인은 배포 환경을 사용합니다.
