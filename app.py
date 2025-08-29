import boto3
from flask import Flask, jsonify, request

app = Flask(__name__)
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("ndhs-api")


@app.route("/items/<string:item_id>", methods=["GET"])
def get_item(item_id):
    response = table.get_item(Key={"id": item_id})
    item = response.get("Item")
    return jsonify(item or {})


@app.route("/items", methods=["POST"])
def create_item():
    data = request.get_json()
    table.put_item(Item=data)
    return jsonify({"message": "Item created"}), 201


if __name__ == "__main__":
    app.run(debug=True)
