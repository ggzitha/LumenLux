import sys
import traceback
import uvicorn

if __name__ == "__main__":
    try:
        print("[run_server] Starting Uvicorn...", flush=True)
        uvicorn.run("app:app", host="0.0.0.0", port=8080, log_level="info", access_log=True)
    except Exception as e:
        with open("server_crash.log", "a", encoding="utf-8") as f:
            f.write(f"Server crashed with: {e}\n")
            traceback.print_exc(file=f)
        traceback.print_exc()
        sys.exit(1)
