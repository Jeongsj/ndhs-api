import html
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_cors import CORS
from google.cloud import firestore
from google.cloud.firestore_v1.transaction import transactional
from google.oauth2 import service_account

load_dotenv()
app = Flask(__name__)
CORS(
    app,
    origins=["https://ndhs.app"],
)

cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
credentials = service_account.Credentials.from_service_account_file(cred_path)
db = firestore.Client(credentials=credentials)


@transactional
def update_post_id_counter(transaction, counter_ref):
    snapshot = counter_ref.get(transaction=transaction)
    count = snapshot.get("count") if snapshot.exists else 0
    count += 1
    transaction.set(counter_ref, {"count": count})
    return count


def increment_post_id_counter(board_id):
    counter_ref = db.collection("counters").document(board_id)
    transaction = db.transaction()
    new_post_id = update_post_id_counter(transaction, counter_ref)
    return str(new_post_id)


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
        return request.headers["X-Forwarded-For"]
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
            # 태그 수신 시 저장 (프론트 글쓰기에서 전송)
            "tag": (data.get("tag") or "").strip(),
            # 기본은 미승인 상태
            "isAccept": False,
        }

    try:
        post_ref = (
            db.collection("boards")
            .document(board_id)
            .collection("posts")
            .document(post_id)
        )
        post_ref.set(post_data)
        return response_json({"message": "Post created", "post_id": post_id}, 201)
    except Exception as e:
        return response_json({"error": str(e)}, 500)


# 게시물 목록 조회 API (페이징 포함)
@app.route("/boards/<board_id>", methods=["GET"])
def get_posts(board_id):
    limit = 10
    last = request.args.get("last")

    posts_ref = db.collection("boards").document(board_id).collection("posts")
    # 공지 이외의 게시판은 승인된 글만 노출
    if board_id != "notice":
        query = (
            posts_ref.where("isAccept", "==", True)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
    else:
        query = posts_ref.order_by(
            "created_at", direction=firestore.Query.DESCENDING
        ).limit(limit)

    if last:
        last_doc = posts_ref.document(last).get()
        if last_doc.exists:
            query = query.start_after(last_doc)

    try:
        docs = query.stream()
        posts = []
        last_id = None
        for doc in docs:
            posts.append({"id": doc.id, **doc.to_dict()})
            last_id = doc.id
        return response_json({"posts": posts, "last": last_id})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


# 게시물 상세 조회 API
@app.route("/boards/<board_id>/<post_id>", methods=["GET"])
def get_post(board_id, post_id):
    post_ref = (
        db.collection("boards").document(board_id).collection("posts").document(post_id)
    )
    try:
        doc = post_ref.get()
        if doc.exists:
            data = doc.to_dict()
            # 공지 외에는 승인된 글만 열람 가능
            if board_id != "notice" and not data.get("isAccept"):
                return response_json({"error": "Post not found"}, 404)
            return response_json({"posts": [data]})
        else:
            return response_json({"error": "Post not found"}, 404)
    except Exception as e:
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
        comment_ref = (
            db.collection("boards")
            .document(board_id)
            .collection("posts")
            .document(post_id)
            .collection("comments")
            .document(comment_id)
        )
        comment_ref.set(comment_data)
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

    comments_ref = (
        db.collection("boards")
        .document(board_id)
        .collection("posts")
        .document(post_id)
        .collection("comments")
    )
    # 승인된 댓글만 노출
    query = (
        comments_ref.where("isAccept", "==", True)
        .order_by("created_at", direction=firestore.Query.ASCENDING)
        .limit(limit)
    )

    if last_comment_id:
        last_doc = comments_ref.document(last_comment_id).get()
        if last_doc.exists:
            query = query.start_after(last_doc)

    try:
        docs = query.stream()
        comments = []
        last_id = None
        for doc in docs:
            comments.append(doc.to_dict())
            last_id = doc.id
        return response_json({"comments": comments, "last_comment_id": last_id})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@transactional
def apply_like_once(transaction, post_ref, ip):
    """Transactionally record a like per IP and increment like counter.

    Returns a dict: {status: 'ok'|'already'|'not_found', likes: int|None}
    """
    doc = post_ref.get(transaction=transaction)
    if not doc.exists:
        return {"status": "not_found", "likes": None}

    like_ref = post_ref.collection("likes").document(ip)
    like_doc = like_ref.get(transaction=transaction)
    if like_doc.exists:
        current_likes = doc.to_dict().get("likes") or 0
        return {"status": "already", "likes": current_likes}

    # Record like by IP and increment counter atomically
    transaction.set(
        like_ref,
        {
            "ip": ip,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    transaction.update(post_ref, {"likes": firestore.Increment(1)})

    new_likes = (doc.to_dict().get("likes") or 0) + 1
    return {"status": "ok", "likes": new_likes}


# 게시물 좋아요 API
@app.route("/boards/<board_id>/<post_id>/like", methods=["POST"])
def like_post(board_id, post_id):
    post_ref = (
        db.collection("boards").document(board_id).collection("posts").document(post_id)
    )

    try:
        # 승인된 글만 추천 가능 (공지 제외)
        doc = post_ref.get()
        if not doc.exists:
            return response_json({"error": "Post not found"}, 404)
        if board_id != "notice" and not (doc.to_dict().get("isAccept")):
            return response_json({"error": "Not acceptable"}, 403)

        transaction = db.transaction()
        ip = get_client_ip()
        result = apply_like_once(transaction, post_ref, ip)
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
    post_ref = (
        db.collection("boards").document(board_id).collection("posts").document(post_id)
    )
    try:
        if not post_ref.get().exists:
            return response_json({"error": "Post not found"}, 404)
        post_ref.update({"isAccept": bool(accept)})
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
    comment_ref = (
        db.collection("boards")
        .document(board_id)
        .collection("posts")
        .document(post_id)
        .collection("comments")
        .document(comment_id)
    )
    try:
        if not comment_ref.get().exists:
            return response_json({"error": "Comment not found"}, 404)
        comment_ref.update({"isAccept": bool(accept)})
        return response_json({"comment_id": comment_id, "isAccept": bool(accept)})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


@app.route("/admin/boards/<board_id>/pending", methods=["GET"])
def admin_list_pending_posts(board_id):
    if not _require_admin():
        return response_json({"error": "Forbidden"}, 403)
    try:
        posts_ref = db.collection("boards").document(board_id).collection("posts")
        q = posts_ref.where("isAccept", "==", False).order_by(
            "created_at", direction=firestore.Query.DESCENDING
        )
        posts = []
        for doc in q.stream():
            d = doc.to_dict()
            d["id"] = doc.id
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
        comments_ref = (
            db.collection("boards")
            .document(board_id)
            .collection("posts")
            .document(post_id)
            .collection("comments")
        )
        q = comments_ref.where("isAccept", "==", False).order_by(
            "created_at", direction=firestore.Query.ASCENDING
        )
        comments = []
        for doc in q.stream():
            d = doc.to_dict()
            d["id"] = doc.id
            comments.append(d)
        return response_json({"items": comments})
    except Exception as e:
        return response_json({"error": str(e)}, 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
