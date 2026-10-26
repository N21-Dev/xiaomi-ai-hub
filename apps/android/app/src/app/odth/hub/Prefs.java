package app.odth.hub;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** 服务器档案与偏好。地址完全由用户填写，App 不内置任何服务器。 */
final class Prefs {

    private static final String FILE = "hub_prefs";
    private static final String KEY_SERVERS = "servers";
    private static final String KEY_LAST = "last";
    private static final String KEY_TAB = "tab";
    private static final String KEY_TOKENS = "tokens";
    private static final String KEY_CSRF = "csrf";
    private static final String KEY_THEME = "theme";   // 0 跟随系统 / 1 浅色 / 2 深色

    private final SharedPreferences sp;
    private final Context app;

    Prefs(Context context) {
        app = context.getApplicationContext();
        sp = app.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    List<ServerConfig> servers() {
        List<ServerConfig> out = new ArrayList<>();
        String raw = sp.getString(KEY_SERVERS, "");
        if (raw == null || raw.isEmpty()) return out;
        try {
            JSONArray array = new JSONArray(raw);
            for (int i = 0; i < array.length(); i++) {
                ServerConfig item = ServerConfig.fromJson(array.optJSONObject(i));
                if (item != null) out.add(item);
            }
        } catch (JSONException ignored) {
            // 配置损坏时当作没有档案，用户重新填一次即可
        }
        return out;
    }

    void save(ServerConfig server) {
        List<ServerConfig> list = servers();
        for (int i = 0; i < list.size(); i++) {
            if (list.get(i).key().equals(server.key())) {
                list.set(i, server);
                persist(list);
                return;
            }
        }
        list.add(0, server);
        while (list.size() > 8) list.remove(list.size() - 1);
        persist(list);
    }

    void remove(ServerConfig server) {
        List<ServerConfig> list = servers();
        for (int i = 0; i < list.size(); i++) {
            if (list.get(i).key().equals(server.key())) {
                list.remove(i);
                break;
            }
        }
        persist(list);
    }

    private void persist(List<ServerConfig> list) {
        JSONArray array = new JSONArray();
        for (ServerConfig item : list) array.put(item.toJson());
        sp.edit().putString(KEY_SERVERS, array.toString()).apply();
    }

    ServerConfig current() {
        List<ServerConfig> list = servers();
        if (list.isEmpty()) return null;
        String key = sp.getString(KEY_LAST, "");
        for (ServerConfig item : list) if (item.key().equals(key)) return item;
        return list.get(0);
    }

    void setCurrent(ServerConfig server) {
        sp.edit().putString(KEY_LAST, server.key()).apply();
    }

    /** 面板登录后的会话令牌，按服务器分开存；过期就重新登录。 */
    String token(ServerConfig server) {
        JSONObject map = tokenMap();
        return map.optString(server.key(), "");
    }

    void setToken(ServerConfig server, String token) {
        write(KEY_TOKENS, server, token);
    }

    private JSONObject tokenMap() {
        try {
            return new JSONObject(sp.getString(KEY_TOKENS, "{}"));
        } catch (JSONException error) {
            return new JSONObject();
        }
    }

    /**
     * 面板的写操作校验码（X-Panel-CSRF），跟会话令牌一一对应。
     * 换一次登录就换一次值，所以必须跟着令牌一起存、一起清。
     */
    String csrf(ServerConfig server) {
        JSONObject map = valueMap(KEY_CSRF);
        return map.optString(server.key(), "");
    }

    void setCsrf(ServerConfig server, String value) {
        write(KEY_CSRF, server, value);
    }

    private JSONObject valueMap(String key) {
        try {
            return new JSONObject(sp.getString(key, "{}"));
        } catch (JSONException error) {
            return new JSONObject();
        }
    }

    private void write(String key, ServerConfig server, String value) {
        JSONObject map = valueMap(key);
        try {
            if (value == null || value.isEmpty()) map.remove(server.key());
            else map.put(server.key(), value);
        } catch (JSONException ignored) {
            return;
        }
        sp.edit().putString(key, map.toString()).apply();
    }

    // ---------------------------------------------------------------- 外观

    static final int THEME_SYSTEM = 0;
    static final int THEME_LIGHT = 1;
    static final int THEME_DARK = 2;

    int themeMode() {
        int mode = sp.getInt(KEY_THEME, THEME_SYSTEM);
        return mode < 0 || mode > 2 ? THEME_SYSTEM : mode;
    }

    void setThemeMode(int mode) {
        sp.edit().putInt(KEY_THEME, mode).apply();
    }

    /** 显式选了就以选择为准，否则看系统的深色开关。 */
    boolean dark() {
        int mode = themeMode();
        if (mode == THEME_LIGHT) return false;
        if (mode == THEME_DARK) return true;
        int night = app.getResources().getConfiguration().uiMode
                & android.content.res.Configuration.UI_MODE_NIGHT_MASK;
        return night == android.content.res.Configuration.UI_MODE_NIGHT_YES;
    }

    int lastTab() {
        return sp.getInt(KEY_TAB, 0);
    }

    void setLastTab(int tab) {
        sp.edit().putInt(KEY_TAB, tab).apply();
    }

    /** 仅在调试与排障时使用，设置页可见。 */
    String dump() {
        JSONArray array = new JSONArray();
        for (ServerConfig item : servers()) array.put(item.toJson());
        return array.toString();
    }

    void clearAll() {
        sp.edit().clear().apply();
    }

    static JSONObject toJsonSafe(ServerConfig server) {
        return server.toJson();
    }
}
