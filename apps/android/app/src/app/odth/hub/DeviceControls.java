package app.odth.hub;

import android.graphics.Typeface;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * 设备控制面板：把 `/api/home` 里 `controls[]` 的四种控件（toggle / range / select / text）
 * 全部渲染出来，和网页端面板对齐。
 *
 * 每个控件都带 `property`，用它去 `device.state` 里取当前值：开关显示开关态、
 * 滑块停在当前亮度、选项高亮当前模式。需要二次确认的控制（`requires_confirmation`）
 * 先弹确认框，确认后才带 `confirmed: true` 发出去。
 */
final class DeviceControls {

    private final MainActivity host;
    private final Api api;
    private final JSONObject device;
    private final String name;
    private final Runnable onChanged;

    private final LinearLayout statusRow;
    private final TextView statusText;
    private final Map<String, Consumer<Object>> selectUpdaters = new HashMap<>();
    private JSONObject state;

    private DeviceControls(MainActivity host, Api api, JSONObject device, String name,
                           Runnable onChanged) {
        this.host = host;
        this.api = api;
        this.device = device;
        this.name = name;
        this.onChanged = onChanged;
        this.state = device.optJSONObject("state");

        statusRow = Ui.row(host);
        statusRow.setVisibility(View.GONE);
        statusText = Ui.meta(host, "");
        statusText.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
    }

    static void open(MainActivity host, Api api, JSONObject device, String name, Runnable onChanged) {
        new DeviceControls(host, api, device, name, onChanged).show();
    }

    private void show() {
        ScrollView scroll = new ScrollView(host);
        scroll.setBackgroundColor(Ui.BG);
        LinearLayout body = Ui.column(host);
        int pad = Ui.dp(host, 18);
        body.setPadding(pad, pad, pad, Ui.dp(host, 8));
        scroll.addView(body);

        TextView title = Ui.text(host, name, 19, Ui.TEXT);
        title.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        TextView close = Ui.textButton(host, "关闭", Ui.ACCENT);
        LinearLayout header = Ui.row(host);
        header.addView(title, Ui.weight(1f));
        header.addView(close);
        body.addView(header);

        TextView sub = Ui.meta(host, device.optBoolean("online", true) ? "在线 · 可以直接控制" : "离线");
        sub.setPadding(0, Ui.dp(host, 4), 0, Ui.dp(host, 8));
        sub.setTextColor(device.optBoolean("online", true) ? Ui.TEXT_3 : Ui.DANGER);
        body.addView(sub);

        statusRow.addView(statusText);
        statusRow.setPadding(0, 0, 0, Ui.dp(host, 8));
        body.addView(statusRow);

        JSONArray controls = Api.array(device, "controls");
        if (controls.length() == 0) {
            TextView empty = Ui.text(host, "这个设备没有可写参数，只能查看状态。", 14, Ui.TEXT_2);
            empty.setPadding(0, Ui.dp(host, 4), 0, Ui.dp(host, 12));
            body.addView(empty);
        } else {
            body.addView(sectionLabel("控制"));
            for (int i = 0; i < controls.length(); i++) {
                JSONObject control = controls.optJSONObject(i);
                if (control != null) body.addView(controlRow(control));
            }
        }

        LinearLayout stateCard = stateCard();
        if (stateCard != null) {
            body.addView(sectionLabel("状态"));
            body.addView(stateCard);
        }

        // 自己画关闭按钮而不是 setNegativeButton：系统按钮栏在深色下是一条突兀的浅灰带
        final android.app.AlertDialog dialog = Ui.dialog(host).setView(scroll).create();
        close.setOnClickListener(view -> dialog.dismiss());
        dialog.show();
        if (dialog.getWindow() != null) {
            dialog.getWindow().setBackgroundDrawable(Ui.round(Ui.BG, 20, host));
        }
    }

    private TextView sectionLabel(String text) {
        TextView view = Ui.section(host, text);
        view.setPadding(0, Ui.dp(host, 12), 0, Ui.dp(host, 6));
        return view;
    }

    // ------------------------------------------------------------ 单个控件

    private View controlRow(JSONObject control) {
        LinearLayout card = Ui.card(host);
        String label = control.optString("label", "参数");
        String kind = control.optString("kind", "text");
        boolean enabled = control.optBoolean("enabled", true);

        LinearLayout header = Ui.row(host);
        TextView title = Ui.text(host, label, 14.5f, enabled ? Ui.TEXT : Ui.TEXT_3);
        header.addView(title, Ui.weight(1f));

        if (!enabled) {
            String reason = control.optString("reason", "设备不支持");
            TextView note = Ui.meta(host, reason);
            note.setPadding(0, Ui.dp(host, 6), 0, 0);
            card.addView(header);
            card.addView(note);
            return card;
        }

        if ("toggle".equals(kind)) {
            boolean on = booleanState(control.optString("property"), control.optBoolean("on_value", true));
            SwitchView power = new SwitchView(host);
            power.setChecked(on, false);
            power.setContentDescription(label);   // 自绘控件默认没有可读名字
            header.addView(power, new LinearLayout.LayoutParams(
                    Ui.dp(host, 48), Ui.dp(host, 48)));
            power.setOnClickListener(view -> {
                boolean target = !power.isChecked();
                Object value = target
                        ? boolValue(control, "on_value", true)
                        : boolValue(control, "off_value", false);
                power.setChecked(target, true);
                send(control, target ? "开启" : "关闭", value, () -> power.setChecked(!target, true));
            });
            card.addView(header);
            return card;
        }

        card.addView(header);

        if ("range".equals(kind)) {
            double min = control.optDouble("min", 0);
            double max = control.optDouble("max", 100);
            double step = control.optDouble("step", 0);
            String unit = control.optString("unit", "");
            double current = doubleState(control.optString("property"), min);
            current = Math.max(min, Math.min(max, current));

            TextView value = Ui.text(host, formatValue(current, step, unit), 13, Ui.ACCENT);
            value.setPadding(0, Ui.dp(host, 6), 0, Ui.dp(host, 2));
            card.addView(value);

            SeekBar bar = new SeekBar(host);
            bar.setContentDescription(label);
            bar.setMax(rangeSteps(min, max, step));
            bar.setProgress(stepOf(current, min, step));
            bar.setProgressTintList(android.content.res.ColorStateList.valueOf(Ui.ACCENT));
            bar.setThumbTintList(android.content.res.ColorStateList.valueOf(Ui.ACCENT));
            bar.setPadding(0, Ui.dp(host, 4), 0, Ui.dp(host, 4));
            bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                @Override
                public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                    value.setText(formatValue(fromStep(progress, min, step), step, unit));
                }

                @Override
                public void onStartTrackingTouch(SeekBar seekBar) {
                }

                @Override
                public void onStopTrackingTouch(SeekBar seekBar) {
                    double target = Math.min(max, Math.max(min,
                            fromStep(seekBar.getProgress(), min, step)));
                    Object payload = step >= 1 ? (Object) Math.round(target) : (Object) target;
                    send(control, value.getText().toString(), payload, null);
                }
            });
            card.addView(bar);
            return card;
        }

        if ("select".equals(kind)) {
            JSONArray options = Api.array(control, "options");
            Object current = stateValue(control.optString("property"));
            card.addView(optionGrid(control, options, current));
            return card;
        }

        // text：数值型给数字键盘，其余按普通文本
        String format = control.optString("format", "string");
        boolean numeric = format.matches("(?i)^(u?int\\d*|float|double|number)$");
        EditText field = new EditText(host);
        field.setContentDescription(label);
        field.setHint(numeric ? "请输入数值" : "请输入");
        field.setSingleLine(true);
        field.setTextSize(15);
        field.setTextColor(Ui.TEXT);
        field.setHintTextColor(Ui.TEXT_3);
        field.setInputType(numeric
                ? InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL
                : InputType.TYPE_CLASS_TEXT);
        card.addView(field);

        TextView apply = Ui.primaryButton(host, "应用");
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = Ui.dp(host, 8);
        apply.setOnClickListener(view -> {
            String raw = field.getText().toString().trim();
            if (raw.isEmpty()) {
                Toast.makeText(host, "请输入内容", Toast.LENGTH_SHORT).show();
                return;
            }
            Object payload = raw;
            if (numeric) {
                try {
                    payload = raw.contains(".") ? (Object) Double.parseDouble(raw) : (Object) Long.parseLong(raw);
                } catch (NumberFormatException error) {
                    Toast.makeText(host, "请输入有效数字", Toast.LENGTH_SHORT).show();
                    return;
                }
            }
            send(control, raw, payload, null);
        });
        card.addView(apply, params);
        return card;
    }

    /** 选项做成小药丸，每行最多三个；选项太多时整块可滚动。 */
    private static final int OPTIONS_PER_ROW = 3;

    private View optionGrid(JSONObject control, JSONArray options, Object current) {
        LinearLayout column = Ui.column(host);
        LinearLayout row = null;
        int inRow = 0;
        List<TextView> chips = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        for (int i = 0; i < options.length(); i++) {
            JSONObject option = options.optJSONObject(i);
            if (option == null) continue;
            if (inRow == 0) {
                row = Ui.row(host);
                LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
                params.topMargin = Ui.dp(host, 8);
                column.addView(row, params);
            }
            Object value = option.opt("value");
            boolean active = sameValue(value, current);
            TextView chip = Ui.chip(host, option.optString("label", String.valueOf(value)),
                    active ? Ui.ACCENT : Ui.TEXT_2,
                    active ? Ui.ACCENT_SOFT : Ui.SURFACE_2);
            chip.setClickable(true);
            chip.setGravity(Gravity.CENTER);
            chip.setMinHeight(Ui.dp(host, 48));   // 触摸目标下限
            Ui.pressable(chip);
            chip.setOnClickListener(view -> send(control, option.optString("label"), value, null));
            chips.add(chip);
            values.add(value);
            LinearLayout.LayoutParams chipParams = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            chipParams.rightMargin = Ui.dp(host, 8);
            if (row != null) row.addView(chip, chipParams);
            inRow++;
            if (inRow >= OPTIONS_PER_ROW) inRow = 0;
        }
        // 最后一行不满时补空位，否则剩下的那一个会被 weight 拉成整行宽
        if (row != null && inRow > 0) {
            for (int i = inRow; i < OPTIONS_PER_ROW; i++) {
                row.addView(new View(host), new LinearLayout.LayoutParams(
                        0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            }
        }
        // 选完之后把高亮挪过去，不用整块重建
        String property = control.optString("property");
        if (!property.isEmpty()) {
            selectUpdaters.put(property, picked -> {
                for (int i = 0; i < chips.size(); i++) {
                    boolean active = sameValue(values.get(i), picked);
                    chips.get(i).setTextColor(active ? Ui.ACCENT : Ui.TEXT_2);
                    chips.get(i).setBackground(Ui.pill(active ? Ui.ACCENT_SOFT : Ui.SURFACE_2, host));
                }
            });
        }
        return column;
    }

    // ------------------------------------------------------------ 状态卡片

    private LinearLayout stateCard() {
        JSONObject current = state;
        if (current == null || current.length() == 0) return null;
        LinearLayout card = Ui.card(host);
        List<String> labels = new ArrayList<>();
        LinearLayout rows = Ui.column(host);
        JSONArray properties = Api.array(device, "properties");
        for (int i = 0; i < properties.length(); i++) {
            JSONObject property = properties.optJSONObject(i);
            if (property != null) labels.add(property.optString("key") + "\u0000"
                    + property.optString("label"));
        }
        boolean first = true;
        for (java.util.Iterator<String> it = current.keys(); it.hasNext(); ) {
            String key = it.next();
            Object value = current.opt(key);
            if (value == null || value == JSONObject.NULL) continue;
            String raw = String.valueOf(value);
            if ("null".equals(raw) || raw.isEmpty()) continue;

            LinearLayout line = Ui.row(host);
            if (!first) line.setPadding(0, Ui.dp(host, 7), 0, 0);
            first = false;
            TextView label = Ui.text(host, labelFor(labels, key), 13.5f, Ui.TEXT_2);
            label.setMaxLines(1);
            TextView display = Ui.text(host, raw, 13.5f, Ui.TEXT);
            display.setGravity(Gravity.END);
            display.setMaxLines(2);
            line.addView(label, Ui.weight(1f));
            line.addView(display);
            rows.addView(line);
        }
        if (first) return null;
        card.addView(rows);
        return card;
    }

    private static String labelFor(List<String> labels, String key) {
        for (String item : labels) {
            String[] parts = item.split("\u0000", 2);
            if (parts[0].equals(key) && parts.length > 1 && !parts[1].isEmpty()) {
                return parts[1];
            }
        }
        return key;
    }

    // ------------------------------------------------------------ 发送

    private void send(JSONObject control, String label, Object value, Runnable revert) {
        String action = control.optString("action", "");
        if (action.isEmpty()) {
            Toast.makeText(host, "这个参数没有可执行的动作", Toast.LENGTH_SHORT).show();
            return;
        }
        boolean confirm = control.optBoolean("requires_confirmation", false);
        if (confirm) {
            String note = control.optString("reason", "");
            Ui.dialog(host)
                    .setTitle("确认操作")
                    .setMessage("确定要对「" + name + "」执行「" + label + "」吗？"
                            + (note.isEmpty() ? "" : "\n" + note))
                    .setNegativeButton("取消", (dialog, which) -> {
                        if (revert != null) revert.run();
                    })
                    .setPositiveButton("确定", (dialog, which) ->
                            dispatch(control, label, value, true, revert))
                    .show();
            return;
        }
        dispatch(control, label, value, false, revert);
    }

    private void dispatch(JSONObject control, String label, Object value,
                          boolean confirmed, Runnable revert) {
        showStatus("正在执行「" + label + "」…", Ui.TEXT_2);
        String key = device.optString("key");
        new Thread(() -> {
            Api.Reply reply = api.control(key, control.optString("action"), value, confirmed);
            host.runOnUiThread(() -> {
                if (!reply.ok()) {
                    if (revert != null) revert.run();
                    showStatus("操作失败：" + reply.message(), Ui.DANGER);
                    Toast.makeText(host, "操作失败：" + reply.message(), Toast.LENGTH_LONG).show();
                    return;
                }
                JSONObject payload = reply.json();
                rememberValue(control, value);
                Values.mergeState(device, payload == null ? null : payload.optJSONObject("state"));
                String outcome = payload == null ? "" : payload.optString("outcome");
                String message = payload == null ? "" : payload.optString("message");
                int color = "confirmed".equals(outcome) ? Ui.ACCENT : Ui.WARN;
                String state = "confirmed".equals(outcome) ? "已执行"
                        : "accepted".equals(outcome) ? "指令已发送" : "已发送，状态待确认";
                showStatus(label + " · " + (message.isEmpty() ? state : message), color);
                if (onChanged != null) onChanged.run();
            });
        }).start();
    }

    /** 本地先记住新值，选项高亮和再次打开时都对得上。 */
    private void rememberValue(JSONObject control, Object value) {
        String property = control.optString("property");
        if (property.isEmpty() || value == null) return;
        if (state == null) state = new JSONObject();
        try {
            state.put(property, value);
        } catch (Exception ignored) {
            return;
        }
        Consumer<Object> updater = selectUpdaters.get(property);
        if (updater != null) updater.accept(value);
    }

    private void showStatus(String text, int color) {
        statusRow.setVisibility(View.VISIBLE);
        statusText.setText(text);
        statusText.setTextColor(color);
    }

    // ------------------------------------------------------------ 值转换

    private Object stateValue(String property) {
        if (property == null || property.isEmpty() || state == null) return null;
        return state.opt(property);
    }

    private boolean booleanState(String property, boolean fallback) {
        if (property == null || property.isEmpty() || state == null) return fallback;
        if (!state.has(property)) return fallback;
        // 可能是 true / 1 / "on" / "开启"，按面板同样的口径解析
        return Values.switchValue(state.opt(property), fallback);
    }

    private double doubleState(String property, double fallback) {
        if (property == null || property.isEmpty() || state == null) return fallback;
        double value = state.optDouble(property, fallback);
        return Double.isNaN(value) ? fallback : value;
    }

    private static Object boolValue(JSONObject control, String key, boolean fallback) {
        Object value = control.opt(key);
        if (value == null || value == JSONObject.NULL) return fallback;
        if (value instanceof Boolean) return value;
        if (value instanceof Number) return ((Number) value).intValue() != 0;
        return String.valueOf(value);
    }

    private static boolean sameValue(Object option, Object current) {
        if (option == null || current == null || current == JSONObject.NULL) return false;
        if (option instanceof Number && current instanceof Number) {
            return ((Number) option).doubleValue() == ((Number) current).doubleValue();
        }
        return String.valueOf(option).equalsIgnoreCase(String.valueOf(current));
    }

    private static int rangeSteps(double min, double max, double step) {
        if (max <= min) return 100;
        int steps = step > 0 ? (int) Math.round((max - min) / step) : 100;
        return Math.max(1, Math.min(steps, 1000));
    }

    private static int stepOf(double value, double min, double step) {
        if (step <= 0) {
            return 0;
        }
        return (int) Math.round((value - min) / step);
    }

    private static double fromStep(int progress, double min, double step) {
        if (step <= 0) return min + progress;
        return min + progress * step;
    }

    private static String formatValue(double value, double step, String unit) {
        String text = step >= 1 || step == 0
                ? String.valueOf(Math.round(value))
                : String.valueOf(Math.round(value * 10) / 10.0);
        return unit.isEmpty() ? text : text + " " + unit;
    }
}
