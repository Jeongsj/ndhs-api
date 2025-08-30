# ndhs-api

## API 명세

| 기능             | 메서드 | URL                                       | 설명                            | 요청 데이터 예시                              |
|----------------|------|-----------------------------------------|-------------------------------|------------------------------------------|
| 게시판 글 작성      | POST | `/boards/<board_id>`                     | 특정 게시판에 새 글 작성               | `{ "title": "제목", "content": "내용", "user_id": "사용자ID", "password": "비밀번호 (notice 게시판시)"}` |
| 게시판 글 목록 조회    | GET  | `/boards/<board_id>`                     | 특정 게시판 글 목록 조회 (최신순, 페이징)    | 쿼리: `?last=마지막글ID` (옵션)                   |
| 게시판 글 상세 조회    | GET  | `/boards/<board_id>/<post_id>`            | 특정 글 상세 조회                    | -                                        |
| 댓글 작성          | POST | `/boards/<board_id>/<post_id>/comments`    | 특정 글에 댓글 작성                   | `{ "content": "댓글 내용", "user_id": "사용자ID" }`            |
| 댓글 목록 조회       | GET  | `/boards/<board_id>/<post_id>/comments`    | 특정 글 댓글 목록 조회 (페이징)          | 쿼리: `?last_comment_id=마지막댓글ID` (옵션)              |


## 페이징 처리

- 목록 조회 시 쿼리 파라미터 `last` 또는 `last_comment_id`를 사용해 Firestore 커서 기반 페이징 지원  
- 첫 페이지 요청 시 쿼리 파라미터 없음 → 최신 게시물(댓글)부터 `limit` 개수 반환  
- 이후 페이지는 마지막 ID를 파라미터로 넣어 다음 페이지 조회


## 인증 및 보안

- `notice` 게시판 글 작성 시 `password` 필드가 환경변수 `NOTICE_PW` 값과 일치해야 허용  
- 다른 게시판은 별도 인증 없음 (추후 확장 가능)


## CORS 설정

- CORS 정책으로 "https://www.ndhs.in" 도메인에서의 요청만 허용


## 프로젝트 참고사항

- Firestore 계층적 컬렉션 구조 사용 (`boards/{board_id}/posts/{post_id}`, `comments` 하위컬렉션)  
- 게시판별 글 ID 카운터 별도 트랜잭션으로 관리  
- `created_at` UTC ISO 8601 형식 타임스탬프 이용 정렬 및 페이징  


## 개발 및 배포

- `GOOGLE_APPLICATION_CREDENTIALS_JSON` 환경변수에 Google 서비스 계정 JSON 문자열을 넣어 인증  
- AWS Lambda, Serverless Framework, GitHub Actions 등 다양한 환경에 맞게 확장 가능
