from asgiref.wsgi import WsgiToAsgi
from mangum import Mangum

from app import app

asgi_app = WsgiToAsgi(app)  # Flask 앱을 ASGI로 감싸기
handler = Mangum(asgi_app)
