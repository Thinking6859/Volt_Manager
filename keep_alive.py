from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def home():
    return "⚡ VOLT Clan Manager (Bolti) is Running!"

def run():
    # Render 호스팅을 통과하기 위한 8080 포트 개방
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()