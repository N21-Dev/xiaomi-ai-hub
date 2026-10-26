package app.odth.hub;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Locale;

/**
 * 一台服务器的连接参数。用户可能只输入 IP，也可能输入带端口的地址，
 * 甚至直接用域名 + 反向代理，所以这里统一归一化成 scheme/host/port 三段。
 */
final class ServerConfig {

    static final int DEFAULT_PANEL_PORT = 18765;
    static final int DEFAULT_HEALTH_PORT = 8321;

    final String scheme;   // http 或 https
    final String host;     // 不含协议、不含端口的地址
    final int panelPort;   // 中枢面板 / 网关
    final int healthPort;  // 健康空间
    final String note;     // 用户自定义别名，可空
    final String username; // 面板登录账号，空表示面板没开登录
    final String password; // 只在内存里，落盘前用 Crypto 加密

    ServerConfig(String scheme, String host, int panelPort, int healthPort, String note) {
        this(scheme, host, panelPort, healthPort, note, "", "");
    }

    ServerConfig(String scheme, String host, int panelPort, int healthPort, String note,
                 String username, String password) {
        this.scheme = scheme;
        this.host = host;
        this.panelPort = panelPort;
        this.healthPort = healthPort;
        this.note = note == null ? "" : note;
        this.username = username == null ? "" : username;
        this.password = password == null ? "" : password;
    }

    boolean hasLogin() {
        return !username.isEmpty() && !password.isEmpty();
    }

    String key() {
        return scheme + "://" + host + ":" + panelPort + ":" + healthPort;
    }

    String label() {
        if (!note.isEmpty()) return note;
        return host + ":" + panelPort;
    }

    String panelBase() {
        return scheme + "://" + authority(host, panelPort);
    }

    String healthBase() {
        return scheme + "://" + authority(host, healthPort);
    }

    /** 省略默认端口，展示更干净，也避免反代域名带端口显得奇怪。 */
    private String authority(String host, int port) {
        if (host.matches(".*:\\d+$")) return host;
        boolean defaultPort =
                ("http".equals(scheme) && port == 80) || ("https".equals(scheme) && port == 443);
        return defaultPort ? host : host + ":" + port;
    }

    JSONObject toJson() {
        JSONObject json = new JSONObject();
        try {
            json.put("scheme", scheme);
            json.put("host", host);
            json.put("panel", panelPort);
            json.put("health", healthPort);
            json.put("note", note);
            json.put("user", username);
            json.put("pass", Crypto.encrypt(password));
        } catch (JSONException ignored) {
            // 字段都是基础类型，不会失败
        }
        return json;
    }

    static ServerConfig fromJson(JSONObject json) {
        if (json == null) return null;
        String host = json.optString("host", "").trim();
        if (host.isEmpty()) return null;
        String scheme = "https".equalsIgnoreCase(json.optString("scheme", "http")) ? "https" : "http";
        int panel = json.optInt("panel", DEFAULT_PANEL_PORT);
        int health = json.optInt("health", DEFAULT_HEALTH_PORT);
        return new ServerConfig(scheme, host, panel, health, json.optString("note", ""),
                json.optString("user", ""), Crypto.decrypt(json.optString("pass", "")));
    }

    /**
     * 解析用户原始输入。支持：
     * 192.168.1.10 / 192.168.1.10:18765 / http://host:port / https://host
     * 也接受粘贴带路径的地址，路径会被忽略。
     */
    static ServerConfig parse(String raw, int healthPort, String note) {
        String text = raw == null ? "" : raw.trim();
        if (text.isEmpty()) return null;
        String scheme = "http";
        int schemeAt = text.indexOf("://");
        if (schemeAt > 0) {
            String candidate = text.substring(0, schemeAt).toLowerCase(Locale.ROOT);
            if (candidate.equals("http") || candidate.equals("https")) scheme = candidate;
            text = text.substring(schemeAt + 3);
        }
        int slash = text.indexOf('/');
        if (slash >= 0) text = text.substring(0, slash);
        text = text.trim();
        if (text.isEmpty()) return null;

        int panelPort = DEFAULT_PANEL_PORT;
        String host = text;
        int colon = text.lastIndexOf(':');
        if (colon > 0 && colon < text.length() - 1 && text.indexOf(']') < colon) {
            String portText = text.substring(colon + 1);
            try {
                int parsed = Integer.parseInt(portText);
                if (parsed > 0 && parsed < 65536) {
                    panelPort = parsed;
                    host = text.substring(0, colon);
                }
            } catch (NumberFormatException ignored) {
                // 不是端口，保留原样（例如 IPv6 里多出来的冒号）
            }
        }
        host = host.replace("[", "").replace("]", "");
        if (host.isEmpty()) return null;
        int health = healthPort > 0 ? healthPort : DEFAULT_HEALTH_PORT;
        return new ServerConfig(scheme, host, panelPort, health, note == null ? "" : note);
    }
}
