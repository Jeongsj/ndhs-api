import html
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import requests
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_cors import CORS

load_dotenv()
app = Flask(__name__)
CORS(
    app,
    origins=["https://ndhs.app"],
)

COSMOS_URI = os.getenv("COSMOS_URI")
COSMOS_KEY = os.getenv("COSMOS_KEY")
COSMOS_DB_NAME = os.getenv("COSMOS_DB_NAME", "ndhs")

# Initialize Cosmos DB client and containers
cosmos_client = CosmosClient(COSMOS_URI, credential=COSMOS_KEY)
database = cosmos_client.create_database_if_not_exists(id=COSMOS_DB_NAME)


def _get_or_create_container(id: str, pk_path: str):
    try:
        return database.create_container_if_not_exists(
            id=id,
            partition_key=PartitionKey(path=pk_path),
        )
    except Exception:
        # If permissions or throughput configuration cause creation to fail, fall back to get_container_client
        return database.get_container_client(id)


posts_container = _get_or_create_container("posts", "/board_id")
comments_container = _get_or_create_container("comments", "/post_id")
counters_container = _get_or_create_container("counters", "/board_id")
likes_container = _get_or_create_container("likes", "/post_id")


def increment_post_id_counter(board_id):
    """Atomically increment per-board post counter using optimistic concurrency (ETag)."""
    # First try to create (new board)
    try:
        counters_container.create_item(
            {
                "id": board_id,
                "board_id": board_id,
                "count": 1,
            }
        )
        return "1"
    except exceptions.CosmosResourceExistsError:
        pass

    # Increment with retry on ETag conflicts
    for _ in range(5):
        try:
            item = counters_container.read_item(item=board_id, partition_key=board_id)
            etag = item.get("_etag")
            item["count"] = (item.get("count") or 0) + 1
            counters_container.replace_item(
                item=board_id,
                body=item,
                if_match=etag,
            )
            return str(item["count"])
        except exceptions.CosmosAccessConditionFailedError:
            # ETag mismatch, retry
            continue
        except exceptions.CosmosHttpResponseError as e:
            raise e
    raise RuntimeError("Failed to increment counter due to concurrent updates")


def response_json(data, status=200):
    # content 필드가 있으면 html.unescape 처리
    def unescape_content(obj):
        if isinstance(obj, dict):
            board_id = obj.get("board_id")
            new_obj = {}
            for k, v in obj.items():
                if k == "content" and isinstance(v, str) and board_id == "notice":
                    new_obj[k] = html.unescape(v)
                else:
                    new_obj[k] = unescape_content(v)
            return new_obj
        elif isinstance(obj, list):
            return [unescape_content(i) for i in obj]
        else:
            return obj

    data = unescape_content(data)
    return (
        Response(
            json.dumps(data, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        ),
        status,
    )


def time_diff(time_str):
    # Use timezone-aware UTC then make KST reference
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=9)
    try:
        # Stored times are naive (assumed KST). Parse and treat as KST naive by
        # comparing against KST naive representation for backward compatibility.
        time_dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%f")
        # Convert aware KST now to naive for consistent subtraction with stored naive times
        time_diff = time_dt - now_kst.replace(tzinfo=None)
    except:
        return 0
    return int(time_diff.total_seconds())


def get_client_ip():
    if "X-Forwarded-For" in request.headers:
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr


# 게시물 작성 API
@app.route("/boards/<board_id>", methods=["POST"])
def create_post(board_id):
    data = request.json
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    ip = get_client_ip()

    if not all([title, content]):
        return response_json({"error": "Missing required fields"}, 400)

    if board_id == "notice":
        password = data.get("password") or ""
        if password != os.getenv("NOTICE_PW"):
            return response_json({"error": "NOT AUTHROIZED!"}, 403)

        post_id = str(data.get("post_id"))
        post_data = {
            "post_id": post_id,
            "board_id": "notice",
            "title": title,
            "content": content,
            "user_id": user_id,
            "created_at": data.get("created_at"),
            "tag": data.get("tag") or "",
            "no": data.get("no"),
            "ip": ip,
            # 공지는 관리자만 작성 → 기본 승인 처리
            "isAccept": True,
        }
    else:
        try:
            post_id = increment_post_id_counter(board_id)
        except Exception as e:
            return response_json({"error": str(e)}, 500)
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Escape user-provided HTML to mitigate XSS for non-notice boards
        content = html.escape(content)
        post_data = {
            "post_id": post_id,
            "board_id": board_id,
            "title": title,
            "content": content,
            "user_id": user_id,
            "created_at": created_at,
            "ip": ip,
            # 기본은 미승인 상태
            "isAccept": False,
        }

    try:
        # Cosmos: posts container, partition by board_id, id = post_id
        post_item = {"id": post_id, **post_data}
        posts_container.upsert_item(post_item)
        return response_json({"message": "Post created", "post_id": post_id}, 201)
    except Exception as e:
        return response_json({"error": str(e)}, 500)


# 게시물 목록 조회 API (페이징 포함)
@app.route("/boards/<board_id>", methods=["GET"])
def get_posts(board_id):
    limit = 10
    last = request.args.get("last")
    last_created_at_param = request.args.get("last_created_at")

    # Keyset pagination by created_at DESC
    last_created_at = last_created_at_param
    if not last_created_at and last:
        try:
            last_item = posts_container.read_item(item=last, partition_key=board_id)
            last_created_at = last_item.get("created_at")
        except exceptions.CosmosResourceNotFoundError:
            last_created_at = None

    params = [
        {"name": "@limit", "value": limit},
    ]
    if last_created_at:
        query = (
            "SELECT TOP @limit c.id, c.post_id, c.board_id, c.title, c.content, c.tag, c.no, c.user_id, "
            "c.created_at, c.isAccept, c.likes "
            "FROM c WHERE c.created_at < @last_created_at "
            "ORDER BY c.created_at DESC"
        )
        params.append({"name": "@last_created_at", "value": last_created_at})
    else:
        query = (
            "SELECT TOP @limit c.id, c.post_id, c.board_id, c.title, c.content, c.tag, c.no, c.user_id, "
            "c.created_at, c.isAccept, c.likes "
            "FROM c ORDER BY c.created_at DESC"
        )

    try:
        items = list(
            posts_container.query_items(
                query=query,
                parameters=params,
                partition_key=board_id,
            )
        )
        posts = []
        last_id = None
        last_created_at_out = None
        for it in items:
            # Ensure id field exists
            if "id" not in it:
                it["id"] = it.get("post_id")
            posts.append(it)
            last_id = it.get("id")
            last_created_at_out = it.get("created_at")
        return response_json(
            {"posts": posts, "last": last_id, "last_created_at": last_created_at_out}
        )
    except Exception as e:
        return response_json({"error": str(e)}, 500)


# 게시물 상세 조회 API
@app.route("/boards/<board_id>/<post_id>", methods=["GET"])
def get_post(board_id, post_id):
    try:
        item = posts_container.read_item(item=post_id, partition_key=board_id)
        # 승인 전 글도 반환하고 프론트에서 마스킹 처리
        return response_json({"posts": [item]})
    except Exception as e:
        if isinstance(e, exceptions.CosmosResourceNotFoundError):
            return response_json({"error": "Post not found"}, 404)
        return response_json({"error": str(e)}, 500)


# 댓글 작성 API
@app.route("/boards/<board_id>/<post_id>/comments", methods=["POST"])
def add_comment(board_id, post_id):
    data = request.json
    content = (data.get("content") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    ip = get_client_ip()

    if not all([content]):
        return response_json({"error": "Missing required field(s)"}, 400)

    comment_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Escape comment content unless on notice board (notice comments may also contain admin HTML)
    safe_content = content if board_id == "notice" else html.escape(content)
    comment_data = {
        "comment_id": comment_id,
        "post_id": post_id,
        "board_id": board_id,
        "content": safe_content,
        "user_id": user_id,
        "created_at": created_at,
        "ip": ip,
        # 댓글도 기본 미승인
        "isAccept": False,
    }

    try:
        comment_item = {"id": comment_id, **comment_data}
        comments_container.upsert_item(comment_item)
        return response_json(
            {"message": "Comment added", "comment_id": comment_id}, 201
        )
    except Exception as e:
        return response_json({"error": str(e)}, 500)


# 댓글 목록 조회 API
@app.route("/boards/<board_id>/<post_id>/comments", methods=["GET"])
def get_comments(board_id, post_id):
    limit = 10
    last_comment_id = request.args.get("last_comment_id")

    # Empty collection quick check via count of top 1
    # 승인 여부와 관계없이 모두 반환 (프론트에서 마스킹)
    last_created_at = None
    if last_comment_id:
        try:
            last_item = comments_container.read_item(
                item=last_comment_id, partition_key=post_id
            )
            last_created_at = last_item.get("created_at")
        except exceptions.CosmosResourceNotFoundError:
            last_created_at = None

    params = [
        {"name": "@post_id", "value": post_id},
        {"name": "@limit", "value": limit},
    ]
    if last_created_at:
        query = (
            "SELECT TOP @limit c.id, c.comment_id, c.post_id, c.board_id, c.content, c.user_id, c.created_at, c.ip, c.isAccept, c.isRejected "
            "FROM c WHERE c.post_id=@post_id AND c.created_at > @last_created_at ORDER BY c.created_at ASC"
        )
        params.append({"name": "@last_created_at", "value": last_created_at})
    else:
        query = (
            "SELECT TOP @limit c.id, c.comment_id, c.post_id, c.board_id, c.content, c.user_id, c.created_at, c.ip, c.isAccept, c.isRejected "
            "FROM c WHERE c.post_id=@post_id ORDER BY c.created_at ASC"
        )

    try:
        items = list(
            comments_container.query_items(
                query=query, parameters=params, partition_key=post_id
            )
        )
        comments = []
        last_id = None
        for it in items:
            comments.append(it)
            last_id = it.get("id")
        return response_json({"comments": comments, "last_comment_id": last_id})
    except Exception as e:
        # 인덱스 미구성 등으로 실패한 경우 폴백: 최대 N개 읽어 정렬/슬라이싱
        err = str(e)
        try:
            fallback_limit = max(20, limit * 3)
            # Read more and sort client-side
            items_all = list(
                comments_container.query_items(
                    query=(
                        "SELECT TOP @limit c.id, c.comment_id, c.post_id, c.board_id, c.content, c.user_id, c.created_at, c.ip, c.isAccept, c.isRejected "
                        "FROM c WHERE c.post_id=@post_id"
                    ),
                    parameters=[
                        {"name": "@limit", "value": fallback_limit},
                        {"name": "@post_id", "value": post_id},
                    ],
                    partition_key=post_id,
                )
            )
            items = []
            from datetime import datetime as _dt

            def _parse(ts):
                try:
                    # stored as ISO string
                    return (
                        _dt.fromisoformat(ts.replace("Z", "+00:00")) if ts else _dt.min
                    )
                except Exception:
                    return _dt.min

            # last cursor 기준시간
            last_created = None
            if last_comment_id:
                try:
                    last_doc = comments_container.read_item(
                        item=last_comment_id, partition_key=post_id
                    )
                    last_created = _parse(last_doc.get("created_at"))
                except exceptions.CosmosResourceNotFoundError:
                    last_created = None

            for data in items_all:
                ctime = _parse(data.get("created_at"))
                if last_created and not (ctime > last_created):
                    continue
                items.append(data)

            items.sort(key=lambda x: _parse(x.get("created_at")))
            sliced = items[:limit]
            next_id = None  # 폴백에서는 안전한 커서 생략
            return response_json({"comments": sliced, "last_comment_id": next_id})
        except Exception as e2:
            return response_json({"error": str(e2)}, 500)


def apply_like_once(post_id, board_id, ip):
    """Transactionally record a like per IP and increment like counter.

    Returns a dict: {status: 'ok'|'already'|'not_found', likes: int|None}
    """
    # 1) Ensure post exists
    try:
        post_item = posts_container.read_item(item=post_id, partition_key=board_id)
    except exceptions.CosmosResourceNotFoundError:
        return {"status": "not_found", "likes": None}

    # 2) Create like record if not exists (partitioned by post_id)
    try:
        likes_container.create_item(
            {
                "id": ip,
                "post_id": post_id,
                "board_id": board_id,
                "ip": ip,
                "created_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    except exceptions.CosmosResourceExistsError:
        current_likes = post_item.get("likes") or 0
        return {"status": "already", "likes": current_likes}

    # 3) Optimistically increment likes on post with ETag retry
    for _ in range(5):
        try:
            # refresh
            post_item = posts_container.read_item(item=post_id, partition_key=board_id)
            etag = post_item.get("_etag")
            post_item["likes"] = (post_item.get("likes") or 0) + 1
            posts_container.replace_item(
                item=post_id,
                body=post_item,
                if_match=etag,
            )
            return {"status": "ok", "likes": post_item.get("likes", 0)}
        except exceptions.CosmosAccessConditionFailedError:
            continue
    # If we failed to increment due to contention, just return current count
    post_item = posts_container.read_item(item=post_id, partition_key=board_id)
    return {"status": "ok", "likes": post_item.get("likes") or 0}


# 게시물 좋아요 API
@app.route("/boards/<board_id>/<post_id>/like", methods=["POST"])
def like_post(board_id, post_id):
    try:
        # 승인된 글만 추천 가능 (공지 제외)
        try:
            post_item = posts_container.read_item(item=post_id, partition_key=board_id)
        except exceptions.CosmosResourceNotFoundError:
            return response_json({"error": "Post not found"}, 404)

        if board_id != "notice" and not (post_item.get("isAccept")):
            return response_json({"error": "Not acceptable"}, 403)

        ip = get_client_ip()
        result = apply_like_once(post_id, board_id, ip)
        if result["status"] == "not_found":
            return response_json({"error": "Post not found"}, 404)

        return response_json(
            {
                "post_id": post_id,
                "likes": result["likes"],
                "already_liked": result["status"] == "already",
            }
        )
    except Exception as e:
        return response_json({"error": str(e)}, 500)


def update_env_file(key, value, file_path=".env"):
    """Update environment variable.

    - In local/dev: write to .env and set process env.
    - In AWS Lambda: use AWS SDK to update the function's environment variables
      and set process env for immediate use in current invocation.
    """
    # If running inside AWS Lambda, update Lambda function configuration
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        try:
            import boto3  # Available in Lambda runtime

            function_name = os.getenv("LAMBDA_FUNCTION_NAME") or os.getenv(
                "AWS_LAMBDA_FUNCTION_NAME"
            )
            region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
            client = (
                boto3.client("lambda", region_name=region)
                if region
                else boto3.client("lambda")
            )

            cfg = client.get_function_configuration(FunctionName=function_name)
            variables = cfg.get("Environment", {}).get("Variables", {}) or {}
            variables[key] = value
            client.update_function_configuration(
                FunctionName=function_name, Environment={"Variables": variables}
            )
            # Reflect in current process env immediately
            os.environ[key] = value
            return True
        except Exception as e:
            print(f"[WARN] Failed to update Lambda env var: {e}")
            # Best-effort: update current process env so rest of handler can continue
            os.environ[key] = value
            return False

    # Default: local/dev, edit .env file
    key_found = False
    new_lines = []
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                key_found = True
            else:
                new_lines.append(line)
    if not key_found:
        new_lines.append(f"{key}={value}\n")
    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    # Also update current process env
    os.environ[key] = value
    return True


def update_laundry_token():
    refreshToken = os.getenv("LAUNDRY_REFRESH_TOKEN")
    url = f"{os.getenv('LAUNDRY_API')}/update-access-token"
    headers = {
        "User-Agent": os.getenv("LAUNDRY_AGENT"),
        "referer": f"{os.getenv('LAUNDRY_REFERER')}/",
        "content-type": "application/json",
        "origin": os.getenv("LAUNDRY_REFERER"),
        "Cookie": f"refreshToken={refreshToken}",
    }

    try:
        print(f"[DEBUG] request to {url}")
        resp = requests.post(url, headers=headers, json={"refreshToken": refreshToken})
        if resp.status_code != 200:
            return response_json(
                {
                    "error": "Update Access-token error",
                    "status": resp.status_code,
                    "text": resp.text,
                },
                502,
            )
        else:
            token = resp.json().get("data", {}).get("accessToken")
            if token:
                update_env_file("LAUNDRY_AUTH", token)
                os.environ["LAUNDRY_AUTH"] = token
                return token
            else:
                return 1
    except requests.RequestException as e:
        return 1


# In-memory cache for laundry results (per sex code)
LAUNDRY_CACHE = {}  # { code: { 'ts': datetime.utcnow(), 'data': [dryers] } }


# 건조기 현황 조회 API
@app.route("/laundry/<sex>", methods=["GET"])
def get_laundry(sex):
    s = sex.strip().lower()
    if s == "m":
        code = "95"
    elif s == "f":
        code = "96"
    else:
        return response_json({"error": "Invalid sex. Use male/female"}, 400)

    # Serve from cache if fresh
    cache_entry = LAUNDRY_CACHE.get(code)
    if cache_entry:
        age = (datetime.now(timezone.utc) - cache_entry["ts"]).total_seconds()
        ttl = int(os.getenv("LAUNDRY_CACHE_TTL", 60))
        if age < ttl:
            cached = cache_entry["data"] or []
            # Recompute time_diff to keep it current without hitting upstream
            dryers = []
            for d in cached:
                dd = dict(d)
                dd["time_diff"] = time_diff(d.get("useEndTime"))
                dryers.append(dd)
            return response_json(dryers)

    token = os.getenv("LAUNDRY_AUTH")
    laundry_api = f"{os.getenv('LAUNDRY_API')}/laundry/new/list"
    url = f"{laundry_api}/{code}"
    headers = {
        "User-Agent": os.getenv("LAUNDRY_AGENT"),
        "referer": f"{os.getenv('LAUNDRY_REFERER')}/",
        "content-type": "application/json",
        "origin": os.getenv("LAUNDRY_REFERER"),
        "authorization": token,
    }
    try:
        print(f"[DEBUG] request to {url}")
        resp = requests.get(url, headers=headers, timeout=3)
        if resp.status_code == 401:  # token expired
            update_laundry_token()
            return get_laundry(s)
        elif resp.status_code != 200:
            return response_json(
                {
                    "error": "Upstream error",
                    "status": resp.status_code,
                    "text": resp.text[:300],
                },
                502,
            )

        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        dryers = []
        for item in items:
            if item.get("equipmentTypeCd") != "DRYER":
                continue
            dryers.append(
                {
                    "equipmentSeq": item.get("equipmentSeq"),
                    "equipmentName": item.get("equipmentName"),
                    "equipmentStatusCd": item.get("equipmentStatusCd"),  # USABLE, USE
                    "equipmentTypeCd": item.get("equipmentTypeCd"),
                    "useEndTime": item.get(
                        "useEndTime"
                    ),  # e.g., 2025-09-01T23:50:02.829 or None
                    "time_diff": time_diff(item.get("useEndTime")),
                }
            )
        # Cache fresh result
        LAUNDRY_CACHE[code] = {"ts": datetime.now(timezone.utc), "data": dryers}
        return response_json(dryers)
    except requests.RequestException as e:
        return response_json({"error": "Request failed", "detail": str(e)}, 502)


# 내 정보 조회 fake API
@app.route("/info/my", methods=["GET"])
def get_info():
    return response_json({"outside": True, "room_id": "126"})


# -----------------------------
# Admin endpoints for moderation
# -----------------------------


def _require_admin():
    token = request.headers.get("X-Admin-Token") or request.args.get("adminToken")
    if not token or token != os.getenv("ADMIN_TOKEN"):
        return False
    return True


@app.route("/admin/boards/<board_id>/<post_id>/accept", methods=["POST"])
def admin_accept_post(board_id, post_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    body = request.json or {}
    accept = body.get("accept")
    if accept is None:
        accept = True
    try:
        try:
            post_item = posts_container.read_item(item=post_id, partition_key=board_id)
        except exceptions.CosmosResourceNotFoundError:
            return response_json({"error": "Post not found"}, 404)
        # apply update
        post_item["isAccept"] = bool(accept)
        if not bool(accept):
            post_item["isRejected"] = True
            post_item["rejected_at"] = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
        else:
            # remove isRejected flags if exist
            post_item.pop("isRejected", None)
            post_item.pop("rejected_at", None)
        posts_container.replace_item(item=post_id, body=post_item)
        return response_json({"post_id": post_id, "isAccept": bool(accept)})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@app.route(
    "/admin/boards/<board_id>/<post_id>/comments/<comment_id>/accept",
    methods=["POST"],
)
def admin_accept_comment(board_id, post_id, comment_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    body = request.json or {}
    accept = body.get("accept")
    if accept is None:
        accept = True
    try:
        try:
            c_item = comments_container.read_item(
                item=comment_id, partition_key=post_id
            )
        except exceptions.CosmosResourceNotFoundError:
            return response_json({"error": "Comment not found"}, 404)
        c_item["isAccept"] = bool(accept)
        if not bool(accept):
            c_item["isRejected"] = True
            c_item["rejected_at"] = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
        else:
            c_item.pop("isRejected", None)
            c_item.pop("rejected_at", None)
        comments_container.replace_item(item=comment_id, body=c_item)
        return response_json({"comment_id": comment_id, "isAccept": bool(accept)})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@app.route("/admin/boards/<board_id>/pending", methods=["GET"])
def admin_list_pending_posts(board_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    try:
        query = (
            "SELECT c.id, c.post_id, c.board_id, c.title, c.content, c.user_id, c.created_at, c.tag, c.no, c.ip, c.isAccept, c.likes, c.isRejected "
            "FROM c WHERE c.board_id=@board_id AND c.isAccept=false "
            "AND (NOT IS_DEFINED(c.isRejected) OR c.isRejected=false) "
            "ORDER BY c.created_at DESC"
        )
        items = list(
            posts_container.query_items(
                query=query,
                parameters=[{"name": "@board_id", "value": board_id}],
                partition_key=board_id,
            )
        )
        posts = []
        for it in items:
            d = dict(it)
            d.setdefault("id", d.get("post_id") or d.get("id"))
            posts.append(d)
        return response_json({"items": posts})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@app.route(
    "/admin/boards/<board_id>/<post_id>/comments/pending",
    methods=["GET"],
)
def admin_list_pending_comments(board_id, post_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    try:
        query = (
            "SELECT c.id, c.comment_id, c.post_id, c.board_id, c.content, c.user_id, c.created_at, c.ip, c.isAccept, c.isRejected "
            "FROM c WHERE c.post_id=@post_id AND c.isAccept=false AND (NOT IS_DEFINED(c.isRejected) OR c.isRejected=false) "
            "ORDER BY c.created_at ASC"
        )
        items = list(
            comments_container.query_items(
                query=query,
                parameters=[{"name": "@post_id", "value": post_id}],
                partition_key=post_id,
            )
        )
        comments = []
        for it in items:
            d = dict(it)
            comments.append(d)
        return response_json({"items": comments})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@app.route("/admin/boards/<board_id>/comments/pending", methods=["GET"])
def admin_list_all_pending_comments(board_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    try:
        # Cross-partition query across comments by board_id
        query = (
            "SELECT c.id, c.comment_id, c.post_id, c.board_id, c.content, c.user_id, c.created_at, c.ip, c.isAccept, c.isRejected "
            "FROM c WHERE c.board_id=@board_id AND c.isAccept=false AND (NOT IS_DEFINED(c.isRejected) OR c.isRejected=false)"
        )
        items = list(
            comments_container.query_items(
                query=query,
                parameters=[{"name": "@board_id", "value": board_id}],
                enable_cross_partition_query=True,
            )
        )
        from datetime import datetime as _dt

        def _parse(ts):
            try:
                return _dt.fromisoformat(ts.replace("Z", "+00:00")) if ts else _dt.min
            except Exception:
                return _dt.min

        items = [d for d in items if not d.get("isRejected")]
        items.sort(key=lambda x: _parse(x.get("created_at")))
        return response_json({"items": items})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
