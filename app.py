import json
import os
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from flask import Flask, Response

load_dotenv()

app = Flask(__name__)

AWS_ACCESS_KEY_ID = os.getenv("AWS_DYNAMODB_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_DYNAMODB_ACCESS_SECRET_KEY")
REGION_NAME = "ap-northeast-2"
TABLE_NAME = "ndhs-notice"

dynamodb = boto3.resource(
    "dynamodb",
    region_name=REGION_NAME,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)
table = dynamodb.Table(TABLE_NAME)


def decimal_default(obj):
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    raise TypeError


def json_response(data, status=200):
    json_data = json.dumps(data, ensure_ascii=False, default=decimal_default)
    # 유니코드 이스케이프 문자를 원래 문자로 복원
    json_data = (
        json_data.replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u0026", "&")
    )
    return Response(
        json_data, status=status, mimetype="application/json; charset=utf-8"
    )


@app.route("/notice", methods=["GET"])
def get_top_notices():
    try:
        response = table.scan()
        items = response.get("Items", [])
        items_sorted = sorted(items, key=lambda x: int(x["id"]), reverse=True)
        top_10 = items_sorted[:10]
        return json_response({"items": top_10})
    except ClientError as e:
        return json_response({"error": e.response["Error"]["Message"]}, status=500)
    except Exception as e:
        return json_response({"error": str(e)}, status=500)


@app.route("/notice/page/<int:pageno>", methods=["GET"])
def get_notices_by_page(pageno):
    try:
        if pageno < 1:
            return json_response(
                {"error": "페이지 번호는 1 이상의 정수여야 합니다."}, 400
            )

        response = table.scan()
        items = response.get("Items", [])
        items_sorted = sorted(items, key=lambda x: int(x["id"]), reverse=True)

        page_size = 10
        start_index = (pageno - 1) * page_size
        end_index = start_index + page_size

        page_items = items_sorted[start_index:end_index]

        if not page_items and pageno != 1:
            return json_response({"error": "해당 페이지에 데이터가 없습니다."}, 404)

        return json_response(
            {
                "page": pageno,
                "page_size": page_size,
                "items": page_items,
                "total_items": len(items),
                "total_pages": (len(items) + page_size - 1) // page_size,
            }
        )
    except ClientError as e:
        return json_response({"error": e.response["Error"]["Message"]}, status=500)
    except Exception as e:
        return json_response({"error": str(e)}, status=500)


@app.route("/notice/<int:no>", methods=["GET"])
def get_notice_by_no(no):
    try:
        id_str = str(no).zfill(20)
        response = table.get_item(Key={"id": id_str, "no": no})
        item = response.get("Item")
        if not item:
            return json_response({"error": "해당 no의 항목이 없습니다."}, 404)
        return json_response(item)
    except Exception as e:
        return json_response({"error": str(e)}, status=500)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
