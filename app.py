import html
import json
import os
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, request
from flask_cors import CORS
from google.cloud import firestore
from google.cloud.firestore_v1.transaction import transactional
from google.oauth2 import service_account

load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://www.ndhs.in"])

gcp_cred_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
credentials_info = json.loads(gcp_cred_json)
credentials = service_account.Credentials.from_service_account_info(credentials_info)
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
            return {
                k: (
                    html.unescape(v)
                    if k == "content" and isinstance(v, str)
                    else unescape_content(v)
                )
                for k, v in obj.items()
            }
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


# 게시물 작성 API
@app.route("/boards/<board_id>", methods=["POST"])
def create_post(board_id):
    data = request.json
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    user_id = (data.get("user_id") or "").strip()

    if not all([title, content, user_id]):
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
        }
    else:
        try:
            post_id = increment_post_id_counter(board_id)
        except Exception as e:
            return response_json({"error": str(e)}, 500)
        created_at = datetime.utcnow().isoformat() + "Z"
        post_data = {
            "post_id": post_id,
            "board_id": board_id,
            "title": title,
            "content": content,
            "user_id": user_id,
            "created_at": created_at,
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
            return response_json({"posts": [doc.to_dict()]})
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

    if not all([content, user_id]):
        return response_json({"error": "Missing required field(s)"}, 400)

    comment_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat() + "Z"
    comment_data = {
        "comment_id": comment_id,
        "post_id": post_id,
        "board_id": board_id,
        "content": content,
        "user_id": user_id,
        "created_at": created_at,
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
    query = comments_ref.order_by(
        "created_at", direction=firestore.Query.ASCENDING
    ).limit(limit)

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


if __name__ == "__main__":
    app.run(debug=True)
