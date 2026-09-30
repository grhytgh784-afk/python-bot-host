import os
import sys
import uuid
import signal
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

import psutil
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent
BOTS_DIR = BASE_DIR / "bots"
LOGS_DIR = BASE_DIR / "logs"
BOTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__)
CORS(app, origins="*")

bots = {}
bots_lock = threading.RLock()


def now():
    return datetime.now(timezone.utc)


def bot_file(bot_id):
    return BOTS_DIR / f"{bot_id}.py"


def bot_log(bot_id):
    return LOGS_DIR / f"{bot_id}.log"


def safe_name(name):
    return Path(name or "bot.py").name.strip() or "bot.py"


def alive(process):
    try:
        return process is not None and process.poll() is None
    except Exception:
        return False


def get_proc(bot):
    process = bot.get("process")
    if not alive(process):
        return None
    try:
        return psutil.Process(process.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def status(bot):
    return "running" if alive(bot.get("process")) else "offline"


def uptime(bot):
    started = bot.get("started_at")
    return max(0, int((now() - started).total_seconds())) if started else 0


def uptime_text(seconds):
    seconds = max(0, int(seconds))
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def proc_stats(bot):
    p = get_proc(bot)
    if not p:
        return 0, 0.0
    try:
        ram = round(p.memory_info().rss / 1024 / 1024, 2)
    except Exception:
        ram = 0
    try:
        cpu = round(p.cpu_percent(interval=None), 2)
    except Exception:
        cpu = 0.0
    return ram, cpu


def read_logs(path, limit=200):
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-limit:])
    except Exception as e:
        return f"[HOST] {e}\n"


def write_log(bot_id, text):
    try:
        with bot_log(bot_id).open("a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception:
        pass


def create_process(bot_id):
    source = bot_file(bot_id)
    log = bot_log(bot_id).open("a", encoding="utf-8", errors="replace")
    log.write(f"\n[HOST] Starting {bot_id} at {now().isoformat()}\n")
    log.flush()

    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True

    return subprocess.Popen([sys.executable, "-u", str(source)], **kwargs)


def stop_process(process):
    if not alive(process):
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def serialize(bot):
    ram, cpu = proc_stats(bot)
    up = uptime(bot)
    return {
        "success": True,
        "id": bot["id"],
        "bot_id": bot["id"],
        "name": bot["name"],
        "filename": bot["filename"],
        "fileName": bot["filename"],
        "status": status(bot),
        "ram": ram,
        "cpu": cpu,
        "uptime": up,
        "uptime_text": uptime_text(up),
        "last_error": bot.get("last_error"),
    }


def get_bot(bot_id):
    with bots_lock:
        return bots.get(bot_id)


def start_bot(bot_id):
    with bots_lock:
        bot = bots.get(bot_id)
        if not bot:
            raise ValueError("Bot not found.")
        if alive(bot.get("process")):
            return
        bot["process"] = create_process(bot_id)
        bot["started_at"] = now()
        bot["last_error"] = None


def stop_bot(bot_id):
    with bots_lock:
        bot = bots.get(bot_id)
        if not bot:
            raise ValueError("Bot not found.")
        stop_process(bot.get("process"))
        bot["process"] = None
        bot["started_at"] = None


def upload_handler():
    uploaded = request.files.get("file") or request.files.get("bot_file")
    if not uploaded:
        return jsonify(success=False, detail="Missing file field."), 400

    filename = safe_name(uploaded.filename)
    if not filename.lower().endswith(".py"):
        return jsonify(success=False, detail="Only .py files are allowed."), 400

    bot_id = str(uuid.uuid4())
    destination = bot_file(bot_id)

    try:
        uploaded.save(destination)
        name = request.form.get("name", "").strip() or filename

        with bots_lock:
            bots[bot_id] = {
                "id": bot_id,
                "name": name,
                "filename": filename,
                "process": None,
                "started_at": None,
                "created_at": now(),
                "last_error": None,
            }

        start_bot(bot_id)
        return jsonify(serialize(bots[bot_id]))
    except Exception as e:
        with bots_lock:
            bots.pop(bot_id, None)
        destination.unlink(missing_ok=True)
        return jsonify(success=False, detail=str(e)), 500


@app.get("/")
def root():
    return jsonify(success=True, service="Python Bot Hosting", status="online")


@app.get("/health")
@app.get("/api/healthz")
def health():
    return jsonify(status="ok")


@app.post("/api/upload")
@app.post("/upload")
@app.post("/api/bots/upload")
def upload():
    return upload_handler()


@app.get("/api/bots")
def list_bots():
    with bots_lock:
        return jsonify([serialize(b) for b in bots.values()])


@app.get("/api/bots/<bot_id>")
def one_bot(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        return jsonify(success=False, detail="Bot not found."), 404
    return jsonify(serialize(bot))


@app.get("/api/bots/<bot_id>/stats")
def bot_stats(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        return jsonify(success=False, detail="Bot not found."), 404
    ram, cpu = proc_stats(bot)
    return jsonify(
        bot_id=bot_id,
        status=status(bot),
        ram_mb=ram,
        cpu_percent=cpu,
        ram=ram,
        cpu=cpu,
        uptime=uptime(bot),
        uptime_text=uptime_text(uptime(bot)),
        last_error=bot.get("last_error"),
    )


@app.post("/api/bots/<bot_id>/start")
def api_start(bot_id):
    try:
        start_bot(bot_id)
        return jsonify(serialize(get_bot(bot_id)))
    except ValueError as e:
        return jsonify(success=False, detail=str(e)), 404
    except Exception as e:
        return jsonify(success=False, detail=str(e)), 500


@app.post("/api/bots/<bot_id>/stop")
def api_stop(bot_id):
    try:
        stop_bot(bot_id)
        return jsonify(serialize(get_bot(bot_id)))
    except ValueError as e:
        return jsonify(success=False, detail=str(e)), 404
    except Exception as e:
        return jsonify(success=False, detail=str(e)), 500


@app.post("/api/bots/<bot_id>/restart")
def api_restart(bot_id):
    try:
        stop_bot(bot_id)
        start_bot(bot_id)
        return jsonify(serialize(get_bot(bot_id)))
    except ValueError as e:
        return jsonify(success=False, detail=str(e)), 404
    except Exception as e:
        return jsonify(success=False, detail=str(e)), 500


@app.delete("/api/bots/<bot_id>")
def delete_bot(bot_id):
    with bots_lock:
        bot = bots.get(bot_id)
        if not bot:
            return jsonify(success=False, detail="Bot not found."), 404

        stop_process(bot.get("process"))
        bot_file(bot_id).unlink(missing_ok=True)
        bot_log(bot_id).unlink(missing_ok=True)
        bots.pop(bot_id, None)

    return jsonify(success=True, bot_id=bot_id)


@app.get("/api/bots/<bot_id>/logs")
def bot_logs(bot_id):
    if not get_bot(bot_id):
        return jsonify(success=False, detail="Bot not found."), 404
    try:
        tail = max(1, min(int(request.args.get("tail", "200")), 5000))
    except ValueError:
        tail = 200
    return jsonify(success=True, bot_id=bot_id, logs=read_logs(bot_log(bot_id), tail))


@app.get("/api/bots/<bot_id>/logs/stream")
def bot_logs_stream(bot_id):
    if not get_bot(bot_id):
        return jsonify(success=False, detail="Bot not found."), 404

    def generate():
        last = None
        while get_bot(bot_id):
            text = read_logs(bot_log(bot_id), 200)
            if text != last:
                last = text
                yield "data: " + __import__("json").dumps(
                    {"logs": text}, ensure_ascii=False
                ) + "\n\n"
            time.sleep(2)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/info")
@app.get("/info")
def info():
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(BASE_DIR))
    cpu = psutil.cpu_percent(interval=0.15)
    return jsonify(
        ram_total=round(memory.total / 1024 / 1024),
        ram_used=round(memory.used / 1024 / 1024),
        ram_percent=int(memory.percent),
        disk_total=round(disk.total / 1024 / 1024),
        disk_used=round(disk.used / 1024 / 1024),
        disk_percent=int(disk.percent),
        cpu_percent=round(cpu, 2),
        uptime=uptime_text(datetime.now().timestamp() - psutil.boot_time()),
    )


@app.get("/api/stats")
def stats():
    with bots_lock:
        active = sum(alive(b.get("process")) for b in bots.values())
        total = len(bots)
    return jsonify(totalBots=total, activeBots=active, systemStatus="مستقر")


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
