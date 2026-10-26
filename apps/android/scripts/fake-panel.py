"""假面板：按 /api/home 的真实契约回一台"什么控制都有"的设备，用来验证客户端控件。

只监听 127.0.0.1，不连任何真实设备。收到 /control 就记录 payload，供验收脚本断言。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS = []

DEVICE = {
    "key": "dev-light-1",
    "name": "客厅主灯",
    "alias": "客厅主灯",
    "room": "客厅",
    "category": "light",
    "model": "demo.light.v1",
    "online": True,
    "enabled": True,
    "state": {
        "on": True,
        "brightness": 62,
        "color_temperature": 3200,
        "mode": "reading",
    },
    "properties": [
        {"key": "on", "label": "开关"},
        {"key": "brightness", "label": "亮度"},
        {"key": "color_temperature", "label": "色温"},
        {"key": "mode", "label": "模式"},
    ],
    "controls": [
        {
            "action": "set_on", "property": "on", "label": "开关", "service": "主灯",
            "kind": "toggle", "on_value": True, "off_value": False,
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_brightness", "property": "brightness", "label": "亮度", "service": "主灯",
            "kind": "range", "min": 1, "max": 100, "step": 1, "unit": "%",
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_color_temp", "property": "color_temperature", "label": "色温",
            "service": "主灯", "kind": "range", "min": 2700, "max": 6500, "step": 100,
            "unit": "K", "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_mode", "property": "mode", "label": "场景模式", "service": "主灯",
            "kind": "select",
            "options": [
                {"value": "reading", "label": "阅读"},
                {"value": "movie", "label": "观影"},
                {"value": "night", "label": "夜灯"},
                {"value": "auto", "label": "自动"},
            ],
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_name", "property": "name", "label": "设备名称", "service": "配置",
            "kind": "text", "format": "string", "max_length": 32,
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_child_lock", "property": "child_lock", "label": "童锁", "service": "配置",
            "kind": "toggle", "on_value": True, "off_value": False,
            "requires_confirmation": False, "enabled": False, "reason": "设备未上报该能力",
        },
    ],
    "capabilities_actions": [],
}

# 空气净化器：真实设备里 on 常常是字符串 "on"，而且列表里第一个 toggle 是「指示灯」而不是电源，
# 这两个细节正是「开了却显示关」的成因，所以特意做成这样。
PURIFIER = {
    "key": "dev-purifier-1",
    "name": "空气净化器",
    "alias": "空气净化器",
    "room": "客厅",
    "category": "air-purifier",
    "model": "demo.purifier.v1",
    "online": True,
    "enabled": True,
    "state": {"on": "on", "aqi": 42, "mode": "auto", "fan_level": 2},
    "properties": [
        {"key": "on", "label": "开关"},
        {"key": "aqi", "label": "空气质量"},
        {"key": "mode", "label": "模式"},
    ],
    "controls": [
        {
            "action": "set_led", "property": "led", "label": "指示灯", "service": "配置",
            "kind": "toggle", "on_value": True, "off_value": False,
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_on", "property": "on", "label": "开关", "service": "净化器",
            "kind": "toggle", "on_value": True, "off_value": False,
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
        {
            "action": "set_mode", "property": "mode", "label": "模式", "service": "净化器",
            "kind": "select",
            "options": [
                {"value": "auto", "label": "自动"},
                {"value": "sleep", "label": "睡眠"},
                {"value": "favorite", "label": "最爱"},
            ],
            "requires_confirmation": False, "enabled": True, "reason": "",
        },
    ],
    "capabilities_actions": [],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._json({"status": "ok"})
        if path == "/api/home":
            return self._json({"devices": [DEVICE, PURIFIER], "audit": []})
        if path == "/api/auth/session":
            return self._json({"enabled": False, "configured": False, "authenticated": False})
        if path == "/__requests":
            return self._json({"requests": REQUESTS})
        if path == "/api/status":
            return self._json({"ready": True, "started_at": "2026-10-20T20:00:00+08:00"})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if path.startswith("/api/home/devices/") and path.endswith("/control"):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            REQUESTS.append({"path": path, "payload": payload})
            # 真实中枢会把回读写进设备资源，之后 /api/home 返回的就是新值；
            # 假面板也要照着改，否则刷新会把界面打回旧状态，测出来的行为是假的。
            readback = {}
            if payload.get("action") == "set_on":
                readback = {"on": payload["value"]}
                for item in (DEVICE, PURIFIER):
                    if item["key"] == path.split("/")[4]:
                        item["state"]["on"] = payload["value"]
            return self._json({
                "ok": True,
                "outcome": "confirmed",
                "message": "设备已确认操作",
                "device_key": path.split("/")[4],
                "state": readback,
            })
        return self._json({"ok": True})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 18770), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("fake panel on 18770", flush=True)
    threading.Event().wait()
