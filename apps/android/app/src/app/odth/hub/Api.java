package app.odth.hub;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;

/** 面板 / 健康服务的接口封装。全部走 Cookie 会话，Cookie 由登录拿到。 */
final class Api {

    static final String COOKIE = "ai_hub_session";
    static final String CSRF_HEADER = "X-Panel-CSRF";

    static final class Reply {
        final int status;
        final String body;
        final String error;

        Reply(int status, String body, String error) {
            this.status = status;
            this.body = body;
            this.error = error;
        }

        boolean ok() {
            return status >= 200 && status < 400 && error == null;
        }

        JSONObject json() {
            return Api.json(body);
        }

        String message() {
            if (error != null) return error;
            JSONObject parsed = json();
            if (parsed != null) {
                String text = parsed.optString("error");
                if (text.isEmpty()) text = parsed.optString("message");
                if (!text.isEmpty()) return text;
            }
            return "HTTP " + status;
        }

        /** 面板用 code 区分失败类型，例如 panel_auth_required / panel_csrf_invalid。 */
        String code() {
            JSONObject parsed = json();
            return parsed == null ? "" : parsed.optString("code", "");
        }
    }

    private static final int CONNECT_TIMEOUT = 4000;
    private static final int READ_TIMEOUT = 12000;

    private final ServerConfig server;
    private final Prefs prefs;

    Api(ServerConfig server, Prefs prefs) {
        this.server = server;
        this.prefs = prefs;
    }

    private String token() {
        return prefs.token(server);
    }

    private String csrf() {
        return prefs.csrf(server);
    }

    // ---------------------------------------------------------------- 基础

    private Reply call(String url, String method, String body, String token, String csrf) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT);
            connection.setReadTimeout(READ_TIMEOUT);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("User-Agent", "HubAndroid/1.2");
            if (token != null && !token.isEmpty()) {
                connection.setRequestProperty("Cookie", COOKIE + "=" + token);
            }
            // 面板对任何写操作都要求带校验码，缺了就是 403「登录校验已更新」。
            if (csrf != null && !csrf.isEmpty() && !"GET".equals(method)) {
                connection.setRequestProperty(CSRF_HEADER, csrf);
            }
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                try (OutputStream out = connection.getOutputStream()) {
                    out.write(body.getBytes(StandardCharsets.UTF_8));
                }
            }
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            return new Reply(status, read(stream), null);
        } catch (Exception error) {
            return new Reply(0, "", friendly(error));
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static String friendly(Exception error) {
        String name = error.getClass().getSimpleName();
        if (name.contains("UnknownHost")) return "找不到服务器";
        if (name.contains("Connect")) return "连不上服务器";
        if (name.contains("Timeout") || name.contains("SocketTimeout")) return "连接超时";
        return name;
    }

    private Reply get(String path) {
        Reply reply = call(server.panelBase() + path, "GET", null, token(), null);
        if (reply.status == 401) {
            prefs.setToken(server, "");
            prefs.setCsrf(server, "");
        }
        return reply;
    }

    /**
     * 所有写操作统一走这里：先保证本机有校验码，服务端说校验码过期就换一次再来。
     * 校验码只在登录时生成，登录一次换一次值，所以不能缓存太久，也不能在 403 后死循环重试。
     */
    private Reply post(String path, JSONObject payload) {
        String url = server.panelBase() + path;
        String body = payload == null ? "{}" : payload.toString();
        String token = token();
        String csrf = csrf();
        if (csrf.isEmpty() && !token.isEmpty()) csrf = refreshSession();
        Reply reply = call(url, "POST", body, token, csrf);
        if (reply.status == 403 && "panel_csrf_invalid".equals(reply.code())) {
            String fresh = refreshSession();
            if (!fresh.isEmpty() && !fresh.equals(csrf)) {
                reply = call(url, "POST", body, token, fresh);
            }
        }
        if (reply.status == 401) {
            // 会话已失效：清掉本地凭据，界面下次渲染会引导重新登录
            prefs.setToken(server, "");
            prefs.setCsrf(server, "");
        }
        return reply;
    }

    /**
     * 读取当前会话的校验码。登录成功后必须调一次，否则第一次写操作会被拒。
     * 返回空串表示没有会话（未登录或已过期），此时顺手清掉本地令牌。
     */
    String refreshSession() {
        String token = token();
        if (token.isEmpty()) return "";
        Reply reply = call(server.panelBase() + "/api/auth/session", "GET", null, token, null);
        if (reply.status == 401) {
            prefs.setToken(server, "");
            prefs.setCsrf(server, "");
            return "";
        }
        JSONObject payload = reply.json();
        if (payload == null || !payload.optBoolean("authenticated", false)) return "";
        String value = payload.optString("csrf", "");
        if (!value.isEmpty()) prefs.setCsrf(server, value);
        return value;
    }

    // ---------------------------------------------------------------- 登录

    Reply login(String username, String password) {
        try {
            JSONObject body = new JSONObject();
            body.put("username", username);
            body.put("password", password);
            body.put("next", "/");
            HttpURLConnection connection = (HttpURLConnection)
                    new URL(server.panelBase() + "/api/auth/login").openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT);
            connection.setReadTimeout(READ_TIMEOUT);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int status = connection.getResponseCode();
            String text = read(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
            String token = cookie(connection.getHeaderFields().get("Set-Cookie"));
            if (status == 200 && token != null) {
                prefs.setToken(server, token);
                prefs.setCsrf(server, "");
            }
            Reply reply = new Reply(status, text, status == 200 && token == null ? "登录成功但没有拿到会话" : null);
            connection.disconnect();
            if (reply.ok()) refreshSession();   // 拿到本次会话的写操作校验码
            return reply;
        } catch (Exception error) {
            return new Reply(0, "", friendly(error));
        }
    }

    private static String cookie(List<String> headers) {
        if (headers == null) return null;
        for (String header : headers) {
            if (header == null) continue;
            for (String part : header.split(";")) {
                String item = part.trim();
                if (item.startsWith(COOKIE + "=")) return item.substring(COOKIE.length() + 1);
            }
        }
        return null;
    }

    boolean hasSession() {
        return !token().isEmpty();
    }

    void logout() {
        post("/api/auth/logout", null);
        prefs.setToken(server, "");
        prefs.setCsrf(server, "");
    }

    // ---------------------------------------------------------------- 数据

    /** /healthz 是公开探针，用来判断「连得上」，不需要登录。 */
    Reply publicStatus() {
        return call(server.panelBase() + "/healthz", "GET", null, null, null);
    }

    Reply home() {
        return get("/api/home");
    }

    Reply control(String key, String action) {
        return control(key, action, null, false);
    }

    /**
     * @param value     参数值：开关传布尔、滑块传数字、选项传对应 value；只读动作传 null
     * @param confirmed 高风险动作必须先由用户确认，服务端也要求这个标记
     */
    Reply control(String key, String action, Object value, boolean confirmed) {
        JSONObject body = new JSONObject();
        try {
            body.put("action", action);
            if (value != null) body.put("value", value);
            if (confirmed) body.put("confirmed", true);
            body.put("request_id", "android-" + System.currentTimeMillis()
                    + "-" + Math.abs(action.hashCode() % 1000));
        } catch (Exception ignored) {
            // 基础类型不会失败
        }
        return post("/api/home/devices/" + urlEncode(key) + "/control", body);
    }

    Reply automations() {
        return get("/api/automations");
    }

    Reply automationAction(String id, String action) {
        JSONObject body = new JSONObject();
        try {
            body.put("action", action);
        } catch (Exception ignored) {
        }
        return post("/api/automations/" + urlEncode(id) + "/action", body);
    }

    Reply chat(String prompt, boolean context, String conversationId) {
        JSONObject body = new JSONObject();
        try {
            body.put("prompt", prompt);
            body.put("context", context);
            body.put("conversation_id", conversationId);
        } catch (Exception ignored) {
        }
        return post("/api/actions/chat", body);
    }

    Reply models() {
        return get("/api/models");
    }

    Reply activate(String providerId, String model, int revision) {
        JSONObject body = new JSONObject();
        try {
            body.put("model", model);
            body.put("revision", revision);
        } catch (Exception ignored) {
        }
        return post("/api/models/" + urlEncode(providerId) + "/activate", body);
    }

    Reply healthSummary(String date) {
        return call(server.healthBase() + "/api/summary?date=" + date, "GET", null, null, null);
    }

    Reply healthSeries(String metric, String start, String end) {
        return call(server.healthBase() + "/api/series/" + metric + "?start=" + start + "&end=" + end,
                "GET", null, null, null);
    }

    // ---------------------------------------------------------------- 工具

    static String urlEncode(String value) {
        try {
            return java.net.URLEncoder.encode(value, "UTF-8").replace("+", "%20");
        } catch (Exception error) {
            return value;
        }
    }

    private static String read(InputStream stream) {
        if (stream == null) return "";
        try (InputStream input = stream; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int read;
            int total = 0;
            while ((read = input.read(buffer)) > 0) {
                total += read;
                if (total > 2 * 1024 * 1024) break;
                out.write(buffer, 0, read);
            }
            return out.toString("UTF-8");
        } catch (Exception error) {
            return "";
        }
    }

    static JSONObject json(String text) {
        if (text == null || text.trim().isEmpty()) return null;
        try {
            return new JSONObject(text);
        } catch (Exception error) {
            return null;
        }
    }

    static JSONArray array(JSONObject object, String key) {
        if (object == null) return new JSONArray();
        JSONArray value = object.optJSONArray(key);
        return value == null ? new JSONArray() : value;
    }
}
