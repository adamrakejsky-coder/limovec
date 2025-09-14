from flask import Flask
from threading import Thread
import os

app = Flask('')

@app.route('/')
def home():
    return "Discord Bot Je Up"

@app.route('/health')
def health():
    return {"status": "healthy", "service": "discord-bot"}

@app.route('/status')
def status():
    return {
        "status": "online",
        "service": "integrated-discord-bot",
        "features": [
            "audit-logging",
            "ticket-system", 
            "invite-tracking",
            "rp-elections"
        ]
    }

def run():
    port = int(os.environ.get('PORT', 8080))  
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True  # Daemon thread for proper shutdown
    t.start()