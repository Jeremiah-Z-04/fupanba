# -*- coding: utf-8 -*-
"""复盘吧 / ThemeHunter 一键启动器（双击 start.bat 或本脚本即可）。

职责：
  1. 把工作目录切到项目根目录，确保 src 包可导入；
  2. 检测 127.0.0.1:8765 是否已有服务在跑，有则直接打开浏览器（幂等）；
  3. 无则启动 FastAPI 服务，待服务就绪后自动打开默认浏览器。

手机访问：`python start.py --lan`
  默认只监听 127.0.0.1（外网/局域网都连不上，最安全）。
  加 --lan 后监听 0.0.0.0，并打印手机能直接打开的局域网地址。
  注意：此时同一 WiFi 下任何设备都能访问，且程序本身没有密码。

仅使用标准库 + 项目依赖，无黑窗口问题（配合 start.bat 用 start 启动）。
"""
import os
import socket
import sys
import threading
import time
import webbrowser

WANT_LAN = "--lan" in sys.argv
HOST = "0.0.0.0" if WANT_LAN else os.environ.get("TH_HOST", "127.0.0.1")
PORT = int(os.environ.get("TH_PORT", "8765") or 8765)
URL = f"http://127.0.0.1:{PORT}"

if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def lan_ip() -> str:
    """取本机在局域网里的地址。连一个外部地址让系统选路，不会真的发包。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def wait_until_ready(host: str, port: int, timeout: float = 20.0) -> bool:
    """轮询等待服务就绪，再打开浏览器，避免白屏。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(host, port):
            return True
        time.sleep(0.2)
    return False


def open_browser():
    try:
        webbrowser.open(URL)
    except Exception as e:
        print(f"自动打开浏览器失败，请手动访问 {URL} （{e}）")


def main() -> int:
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, PROJECT_ROOT)

    # 已在运行：直接打开浏览器（0.0.0.0 不能直接连，统一探 127.0.0.1）
    if port_in_use("127.0.0.1", PORT):
        if WANT_LAN:
            # 关键：端口被占用也可能是「只监听本机」的旧实例，那样手机照样连不上，
            # 不能直接说「已在运行」把用户糊弄过去。用局域网 IP 反探一次才知道真相。
            ip = lan_ip()
            if ip and port_in_use(ip, PORT):
                print(f"复盘吧已在运行（局域网模式）")
                print(f"手机访问：http://{ip}:{PORT}   （手机需连同一个 WiFi）")
                open_browser()
                return 0
            print(f"8765 端口被一个只监听本机的实例占用，手机连不上。")
            print(f"请先关掉原来那个复盘吧窗口（或结束占用 {PORT} 端口的 python 进程），再运行本程序。")
            return 1
        print(f"复盘吧已在运行，打开 {URL}")
        open_browser()
        return 0

    print("正在启动复盘吧……")
    if HOST == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"手机访问：http://{ip}:{PORT}   （手机需连同一个 WiFi）")
        print("注意：已开启局域网访问，同一网络下的设备都能打开本页。")
    try:
        import uvicorn
        from src.log import setup
        from src.config import Config
        from src.server.app import create_app

        cfg = Config.load()
        setup(cfg.data_dir / "logs")
    except ImportError as e:
        print(f"缺少依赖，请先安装：pip install -r requirements.txt\n错误：{e}")
        input("按回车退出……")
        return 1

    # 服务就绪后自动开浏览器（后台线程，不阻塞启动）
    if os.environ.get("TH_NO_BROWSER") != "1":
        threading.Thread(target=lambda: (wait_until_ready("127.0.0.1", PORT), open_browser()),
                         daemon=True).start()

    try:
        uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")
    except OSError as e:
        print(f"启动失败：{e}")
        print(f"若端口 {PORT} 被占用，可改 src 或 start.py 里的 PORT。")
        input("按回车退出……")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
