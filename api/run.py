import os

from flask import Flask, jsonify, request

from bid_bot import main

# Deployment marker: refresh Vercel environment variables.
app = Flask(__name__)


def _authorized():
    expected = os.environ.get("CRON_SECRET")
    supplied = request.headers.get("Authorization", "")
    return bool(expected) and supplied == f"Bearer {expected}"


@app.route("/", methods=["GET", "POST"])
@app.route("/api/run", methods=["GET", "POST"])
def run_bid_bot():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        result = main()
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/health", methods=["GET"])
@app.route("/api/run/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "region": os.environ.get("VERCEL_REGION"),
    })
