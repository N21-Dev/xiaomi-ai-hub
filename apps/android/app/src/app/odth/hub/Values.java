package app.odth.hub;

import org.json.JSONObject;

import java.util.Locale;

/**
 * 设备状态值的容错解析。
 *
 * 同一个「开关」在不同设备上可能是布尔、数字 0/1，也可能是 "on"/"off" 这样的字符串
 * （网页端 `homePower()` 就是这么处理的）。直接 `optBoolean("on")` 会把 "on" 和 1
 * 全判成 false，表现就是「明明开了却显示关闭」。
 */
final class Values {

    private Values() {
    }

    /** 未知返回 null，调用方据此决定要不要显示状态。 */
    static Boolean switchValue(Object raw) {
        if (raw == null || raw == JSONObject.NULL) return null;
        if (raw instanceof Boolean) return (Boolean) raw;
        if (raw instanceof Number) return ((Number) raw).doubleValue() != 0;
        String text = String.valueOf(raw).trim().toLowerCase(Locale.ROOT);
        if (text.isEmpty()) return null;
        if (text.equals("on") || text.equals("true") || text.equals("1")
                || text.equals("open") || text.equals("开启") || text.equals("打开")) {
            return Boolean.TRUE;
        }
        if (text.equals("off") || text.equals("false") || text.equals("0")
                || text.equals("close") || text.equals("closed")
                || text.equals("关闭")) {
            return Boolean.FALSE;
        }
        return null;
    }

    /** 开关类状态：认不出来时退回 fallback。 */
    static boolean switchValue(Object raw, boolean fallback) {
        Boolean value = switchValue(raw);
        return value == null ? fallback : value;
    }

    /** 把控制接口回读的 state 合并进设备对象，让界面立刻反映结果。 */
    static void mergeState(JSONObject device, JSONObject readback) {
        if (device == null || readback == null || readback.length() == 0) return;
        JSONObject state = device.optJSONObject("state");
        if (state == null) {
            state = new JSONObject();
            try {
                device.put("state", state);
            } catch (Exception ignored) {
                return;
            }
        }
        java.util.Iterator<String> keys = readback.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            Object value = readback.opt(key);
            if (value == null || value == JSONObject.NULL) continue;
            try {
                state.put(key, value);
            } catch (Exception ignored) {
                // 单个字段写不进去不影响其它字段
            }
        }
    }
}
