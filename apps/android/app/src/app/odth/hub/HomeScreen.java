package app.odth.hub;

import android.app.AlertDialog;
import android.graphics.Typeface;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 首页：按房间分组的设备卡片，手机单列、平板两列/三列。 */
final class HomeScreen implements Screen {

    private final MainActivity host;
    private final Api api;
    private final ScrollView scroll;
    private final LinearLayout content;
    private final TextView summary;
    /** 设备 key -> 卡片上的开关/状态文字，控制成功后就地更新，避免整页重建跳回顶部。 */
    private final Map<String, SwitchView> powerViews = new LinkedHashMap<>();
    private final Map<String, TextView> statusViews = new LinkedHashMap<>();

    HomeScreen(MainActivity host, Api api) {
        this.host = host;
        this.api = api;

        scroll = new ScrollView(host);
        scroll.setBackgroundColor(Ui.BG);

        content = Ui.column(host);
        int gutter = Theme.gutter(host);
        content.setPadding(Ui.dp(host, gutter), Ui.dp(host, 4),
                Ui.dp(host, gutter), Ui.dp(host, 20));

        summary = Ui.meta(host, "正在读取设备…");
        summary.setPadding(Ui.dp(host, 4), 0, 0, Ui.dp(host, 10));
        content.addView(summary);

        scroll.addView(content);
    }

    @Override
    public View view() {
        return scroll;
    }

    @Override
    public void refresh() {
        summary.setText("正在读取设备…");
        new Thread(() -> {
            Api.Reply reply = api.home();
            host.runOnUiThread(() -> render(reply));
        }).start();
    }

    private void render(Api.Reply reply) {
        if (host.isFinishing()) return;
        content.removeAllViews();
        powerViews.clear();
        statusViews.clear();
        summary.setTextColor(Ui.TEXT_3);
        content.addView(summary);

        if (!reply.ok()) {
            summary.setText(reply.status == 401 ? "面板需要登录才能读取设备。" : "读取失败：" + reply.message());
            summary.setTextColor(reply.status == 401 ? Ui.WARN : Ui.DANGER);
            if (reply.status == 401) {
                TextView action = Ui.primaryButton(host, "立即登录");
                action.setOnClickListener(view -> host.promptLogin());
                content.addView(action, new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            }
            return;
        }

        JSONObject payload = reply.json();
        JSONArray devices = Api.array(payload, "devices");
        if (devices.length() == 0) {
            summary.setText("还没有设备。到服务器上完成米家登录与设备发现。");
            return;
        }

        Map<String, List<JSONObject>> rooms = new LinkedHashMap<>();
        int online = 0;
        for (int i = 0; i < devices.length(); i++) {
            JSONObject device = devices.optJSONObject(i);
            if (device == null) continue;
            if (device.optBoolean("online")) online++;
            String room = device.optString("room");
            if (room.isEmpty()) room = "未分组";
            rooms.computeIfAbsent(room, key -> new ArrayList<>()).add(device);
        }
        summary.setText(devices.length() + " 台设备 · " + online + " 台在线");

        int columns = Theme.columns(host);
        int gutter = Ui.dp(host, 12);
        int rowIndex = 0;

        for (Map.Entry<String, List<JSONObject>> entry : rooms.entrySet()) {
            LinearLayout header = Ui.row(host);
            header.setPadding(Ui.dp(host, 4), Ui.dp(host, 10), Ui.dp(host, 4), Ui.dp(host, 6));
            header.addView(Ui.section(host, entry.getKey()), new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            header.addView(Ui.meta(host, entry.getValue().size() + " 台"));
            content.addView(header);

            List<JSONObject> items = entry.getValue();
            for (int start = 0; start < items.size(); start += columns) {
                LinearLayout row = Ui.row(host);
                row.setGravity(Gravity.TOP);
                LinearLayout.LayoutParams rowParams = new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
                rowParams.bottomMargin = gutter;
                for (int column = 0; column < columns; column++) {
                    int position = start + column;
                    LinearLayout.LayoutParams cell = new LinearLayout.LayoutParams(
                            0, ViewGroup.LayoutParams.MATCH_PARENT, 1f);
                    if (column < columns - 1) cell.rightMargin = gutter;
                    if (position < items.size()) {
                        row.addView(deviceTile(items.get(position)), cell);
                    } else {
                        row.addView(new View(host), cell);
                    }
                }
                content.addView(row, rowParams);
                Ui.stagger(row, rowIndex++);
            }
        }
    }

    private View deviceTile(JSONObject device) {
        LinearLayout tile = Ui.tile(host);

        boolean online = device.optBoolean("online");
        String deviceKey = device.optString("key");
        String rawName = device.optString("alias");
        if (rawName.isEmpty()) rawName = device.optString("name", "未命名设备");
        final String name = rawName;

        LinearLayout top = Ui.row(host);
        View icon = iconCircle(categoryGlyph(device.optString("category")), online);
        top.addView(icon, new LinearLayout.LayoutParams(Ui.dp(host, 40), Ui.dp(host, 40)));

        JSONObject toggle = toggleControl(device);
        JSONObject state = device.optJSONObject("state");
        // 开关值可能是 true / 1 / "on" / "开启"，不能只认布尔
        boolean on = state != null && Values.switchValue(state.opt("on"), false);
        top.addView(new View(host), new LinearLayout.LayoutParams(0, 1, 1f));
        if (toggle != null) {
            SwitchView power = new SwitchView(host);
            power.setChecked(on, false);
            power.setContentDescription(name + " 开关");
            power.setOnClickListener(view -> {
                boolean target = !power.isChecked();
                power.setChecked(target, true);
                // 缺 off_action 时不能硬写成 turn_off —— 那会打到别的动作上
                String action = toggle.optString("action", target ? "turn_on" : "turn_off");
                if (!target) {
                    String offAction = toggle.optString("off_action");
                    if (!offAction.isEmpty()) action = offAction;
                }
                Object value = target
                        ? toggle.opt("on_value")
                        : toggle.opt("off_value");
                if (value == null || value == JSONObject.NULL) value = target;
                control(device, name, action, value, power, !target);
            });
            top.addView(power, new LinearLayout.LayoutParams(Ui.dp(host, 48), Ui.dp(host, 48)));
            if (!deviceKey.isEmpty()) powerViews.put(deviceKey, power);
        } else {
            top.addView(Ui.chip(host, online ? "在线" : "离线",
                    online ? Ui.ACCENT : Ui.TEXT_3,
                    online ? Ui.ACCENT_SOFT : Ui.SURFACE_2));
        }
        tile.addView(top);

        TextView title = Ui.text(host, name, 15.5f, Ui.TEXT);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        title.setPadding(0, Ui.dp(host, 12), 0, 0);
        title.setMaxLines(1);
        tile.addView(title);

        int controlCount = Api.array(device, "controls").length();
        String detail = statusText(device, online);
        if (online && controlCount > 1) detail += " · 可调 " + controlCount + " 项";
        TextView status = Ui.text(host, detail, 12, online ? Ui.TEXT_2 : Ui.TEXT_3);
        status.setPadding(0, Ui.dp(host, 4), 0, 0);
        status.setMaxLines(2);
        tile.addView(status);
        if (!deviceKey.isEmpty()) statusViews.put(deviceKey, status);

        Ui.pressable(tile);
        tile.setOnClickListener(view -> DeviceControls.open(host, api, device, name, this::refresh));
        return tile;
    }

    private View iconCircle(String glyph, boolean online) {
        LinearLayout circle = Ui.column(host);
        circle.setGravity(Gravity.CENTER);
        circle.setBackground(Ui.round(online ? Ui.ACCENT_SOFT : Ui.SURFACE_2, 14, host));
        GlyphView view = new GlyphView(host, glyph, online ? Ui.ACCENT : Ui.TEXT_3);
        circle.addView(view, new LinearLayout.LayoutParams(Ui.dp(host, 22), Ui.dp(host, 22)));
        return circle;
    }

    /** 控制返回了回读状态时，先就地更新这张卡，再拉一次全量数据对齐其它设备。 */
    private void applyReadback(JSONObject device, JSONObject payload) {
        if (payload == null) return;
        Values.mergeState(device, payload.optJSONObject("state"));
        String key = device.optString("key");
        SwitchView power = powerViews.get(key);
        Boolean on = Values.switchValue(
                device.optJSONObject("state") == null
                        ? null
                        : device.optJSONObject("state").opt("on"));
        if (power != null && on != null) power.setChecked(on, true);
        TextView status = statusViews.get(key);
        if (status != null) {
            boolean online = device.optBoolean("online", true);
            int controlCount = Api.array(device, "controls").length();
            String detail = statusText(device, online);
            if (online && controlCount > 1) detail += " · 可调 " + controlCount + " 项";
            status.setText(detail);
        }
    }

    private void control(JSONObject device, String name, String action, Object value,
                         SwitchView power, boolean revertTo) {
        String key = device.optString("key");
        new Thread(() -> {
            Api.Reply reply = api.control(key, action, value, false);
            host.runOnUiThread(() -> {
                if (reply.ok()) {
                    JSONObject payload = reply.json();
                    applyReadback(device, payload);
                    String outcome = payload == null ? "" : payload.optString("outcome");
                    Toast.makeText(host,
                            "confirmed".equals(outcome) ? name + " 已响应" : "指令已发送",
                            Toast.LENGTH_SHORT).show();
                    // 稍等一下再拉全量，避免服务端还没落盘回读就把界面打回旧值
                    scroll.postDelayed(this::refresh, 600);
                } else {
                    power.setChecked(revertTo, true);
                    Toast.makeText(host, "操作失败：" + reply.message(), Toast.LENGTH_LONG).show();
                }
            });
        }).start();
    }

    /**
     * 卡片上的快捷开关只应该控制电源。
     * 不能直接拿第一个 toggle —— 空气净化器的第一个开关可能是童锁或提示音。
     */
    private static JSONObject toggleControl(JSONObject device) {
        JSONArray controls = Api.array(device, "controls");
        JSONObject fallback = null;
        for (int i = 0; i < controls.length(); i++) {
            JSONObject control = controls.optJSONObject(i);
            if (control == null || !"toggle".equals(control.optString("kind"))) continue;
            String property = control.optString("property");
            if ("on".equals(property) || "power".equals(property)) return control;
            if (fallback == null) fallback = control;
        }
        return fallback;
    }

    private static String statusText(JSONObject device, boolean online) {
        if (!online) return "离线";
        JSONObject state = device.optJSONObject("state");
        if (state == null) return "在线";
        Boolean power = state == null ? null : Values.switchValue(state.opt("on"));
        if (power != null) {
            List<String> parts = new ArrayList<>();
            parts.add(power ? "已开启" : "已关闭");
            if (state.has("brightness") && state.optInt("brightness") > 0) {
                parts.add("亮度 " + Math.round(state.optInt("brightness") / 2.55) + "%");
            }
            if (state.has("temperature")) parts.add(state.optInt("temperature") + "℃");
            return String.join(" · ", parts);
        }
        return "在线";
    }

    private static String categoryGlyph(String category) {
        String value = category == null ? "" : category.toLowerCase(java.util.Locale.ROOT);
        if (value.contains("light") || value.contains("lamp")) return GlyphView.BULB;
        if (value.contains("plug") || value.contains("socket") || value.contains("outlet")) return GlyphView.PLUG;
        if (value.contains("air") || value.contains("conditioner") || value.contains("fan")) return GlyphView.AC;
        if (value.contains("router") || value.contains("gateway")) return GlyphView.ROUTER;
        if (value.contains("sensor") || value.contains("switch")) return GlyphView.SENSOR;
        return GlyphView.DEVICE;
    }
}
