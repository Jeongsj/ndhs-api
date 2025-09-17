# ndhs-api

## API 명세

| 기능                   | 메서드 | URL                                                               | 설명                                      | 요청 데이터 예시                                        |
| ---------------------- | ------ | ----------------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------- | -------- |
| 게시판 글 작성         | POST   | `/boards/<board_id>`                                              | 특정 게시판에 새 글 작성 (기본 미승인)    | `{ "title": "제목", "content": "내용", "tag": "분류" }` |
| 게시판 글 목록 조회    | GET    | `/boards/<board_id>`                                              | 특정 게시판 글 목록 조회 (최신순, 페이징) | 쿼리: `?last=마지막글ID` (옵션)                         |
| 게시판 글 상세 조회    | GET    | `/boards/<board_id>/<post_id>`                                    | 특정 글 상세 조회 (미승인 글은 404)       | -                                                       |
| 댓글 작성              | POST   | `/boards/<board_id>/<post_id>/comments`                           | 특정 글에 댓글 작성 (기본 미승인)         | `{ "content": "댓글 내용" }`                            |
| 댓글 목록 조회         | GET    | `/boards/<board_id>/<post_id>/comments`                           | 특정 글 승인된 댓글 목록 조회 (페이징)    | 쿼리: `?last_comment_id=마지막댓글ID` (옵션)            |
| 글 승인/반려(관리자)   | POST   | `/admin/boards/<board_id>/<post_id>/accept`                       | 관리자 토큰으로 글 승인/반려              | 헤더: `X-Admin-Token`, 바디: `{ "accept": true          | false }` |
| 댓글 승인/반려(관리자) | POST   | `/admin/boards/<board_id>/<post_id>/comments/<comment_id>/accept` | 관리자 토큰으로 댓글 승인/반려            | 헤더: `X-Admin-Token`, 바디: `{ "accept": true          | false }` |
| 대기 글 목록(관리자)   | GET    | `/admin/boards/<board_id>/pending`                                | 미승인 글 목록 조회(최신순)               | 헤더: `X-Admin-Token`                                   |
| 대기 댓글 목록(관리자) | GET    | `/admin/boards/<board_id>/<post_id>/comments/pending`             | 특정 글의 미승인 댓글 목록 조회           | 헤더: `X-Admin-Token`                                   |

## 페이징 처리

- 목록 조회 시 쿼리 파라미터 `last` 또는 `last_comment_id` 사용
- 첫 페이지: 파라미터 없음 → 최신순(게시물은 DESC, 댓글은 ASC) `limit` 개 반환
- 다음 페이지: `last`(또는 `last_comment_id`) 아이템의 `created_at` 기준으로 키셋 페이지네이션

## 인증 및 보안

- `notice` 게시판 글 작성 시 `password` 필드가 환경변수 `NOTICE_PW` 값과 일치해야 허용
- 일반 게시판 글/댓글은 기본값 `isAccept=false`로 저장되며, 관리자 승인 이후(`isAccept=true`)에만 목록/상세/댓글 조회에 노출됩니다. 공지는 작성 즉시 승인됩니다.
- 관리자 엔드포인트는 요청 헤더 `X-Admin-Token: <ADMIN_TOKEN>` 또는 쿼리 `?adminToken=<ADMIN_TOKEN>`이 필요합니다.

## CORS 설정

- CORS 정책으로 "https://ndhs.app" 도메인에서의 요청만 허용

## 프로젝트 참고사항

- Azure Cosmos DB for NoSQL 사용
- 컨테이너 및 파티션키
  - posts: 파티션키 `/board_id`, 문서 `id=post_id`
  - comments: 파티션키 `/post_id`, 문서 `id=comment_id`
  - counters: 파티션키 `/board_id`, 문서 `id=board_id` (게시판별 글번호 카운터)
  - likes: 파티션키 `/post_id`, 문서 `id=ip` (게시물당 IP 1회 제한)
- `created_at` UTC ISO 8601 문자열로 정렬/페이징
- 기본 인덱싱으로 단일 속성 정렬(ORDER BY) 지원, 크로스 파티션 정렬은 사용하지 않음

## 개발 및 배포

- 환경변수
  - `COSMOS_URI`: Cosmos DB 계정 URI
  - `COSMOS_KEY`: Cosmos DB 계정 키(Primary Key)
  - `COSMOS_DB_NAME`: 데이터베이스 이름(기본값 `ndhs`)
  - `ADMIN_TOKEN`: 관리자 토큰
  - `NOTICE_PW`: 공지 작성 비밀번호
- AWS Lambda, Serverless Framework, GitHub Actions 등 다양한 환경에 맞게 확장 가능
