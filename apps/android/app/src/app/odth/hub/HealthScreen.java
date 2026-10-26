package app.odth.hub;

import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/** 健康：今日指标卡 + 近 7 天柱状图 + 睡眠结构。宽屏指标排成一行四列。 */
final class HealthScreen implements Screen {

    private final MainActivity host;
    private final Api api;
    private final ScrollView scroll;
    private final LinearLayout list;
    private final TextView subtitle;

    HealthScreen(MainActivity host, Api api) {
        this.host = host;
        this.api = api;

        scroll = new ScrollView(host);
        scroll.setBackgroundColor(Ui.BG);
        list = Ui.column(host);
        int gutter = Ui.dp(host, Theme.gutter(host));
        list.setPadding(gutter, Ui.dp(host, 4), gutter, Ui.dp(host, 24));

        subtitle = Ui.meta(host, "读取中…");
        subtitle.setPadding(Ui.dp(host, 4), 0, 0, Ui.dp(host, 8));
        list.addView(subtitle);

        scroll.addView(list);
    }

    @Override
    public View view() {
        return scroll;
    }

    @Override
    public void refresh() {
        subtitle.setText("读取中…");
        new Thread(() -> {
            String today = day(0);
            Api.Reply summary = api.healthSummary(today);
            JSONObject todayPayload = summary.json();
            boolean needsSleep = todayPayload == null || todayPayload.optJSONObject("sleep") == null;
            Api.Reply yesterday = needsSleep ? api.healthSummary(day(1)) : null;
            Api.Reply series = api.healthSeries("steps", day(6), today);
            host.runOnUiThread(() -> render(summary, yesterday, series, today));
        }).start();
    }

    private void render(Api.Reply summary, Api.Reply yesterday, Api.Reply series, String today) {
        if (host.isFinishing()) return;
        list.removeAllViews();
        list.addView(subtitle);

        if (!summary.ok()) {
            subtitle.setText(summary.status == 0
                    ? "健康服务连不上：" + summary.message()
                    : "健康服务返回 " + summary.status + "：" + summary.message());
            subtitle.setTextColor(Ui.DANGER);
            return;
        }

        JSONObject payload = summary.json();
        JSONObject activity = payload == null ? null : payload.optJSONObject("activity");
        JSONObject sleep = payload == null ? null : payload.optJSONObject("sleep");
        JSONObject resting = payload == null ? null : payload.optJSONObject("resting_hr");
        JSONObject workout = payload == null ? null : payload.optJSONObject("workout");

        String sleepFrom = today;
        if (sleep == null && yesterday != null) {
            JSONObject older = yesterday.json();
            sleep = older == null ? null : older.optJSONObject("sleep");
            if (sleep != null) sleepFrom = day(1);
        }

        subtitle.setText(today);
        subtitle.setTextColor(Ui.TEXT_3);

        if (activity == null && sleep == null && resting == null && workout == null) {
            LinearLayout empty = Ui.card(host);
            empty.addView(Ui.cardTitle(host, "这一天还没有同步数据"));
            TextView hint = Ui.meta(host, "到服务器上触发一次健康同步即可。");
            hint.setPadding(0, Ui.dp(host, 6), 0, 0);
            empty.addView(hint);
            list.addView(empty);
        } else {
            list.addView(metricGrid(activity, sleep, resting, workout));
        }

        View chart = chartCard(series);
        list.addView(chart);
        Ui.enter(chart, 120);
        if (sleep != null) list.addView(sleepCard(sleep, sleepFrom));
    }

    private View metricGrid(JSONObject activity, JSONObject sleep, JSONObject resting, JSONObject workout) {
        int columns = Theme.wide(host) ? 4 : 2;
        int gutter = Ui.dp(host, 12);
        LinearLayout grid = Ui.column(host);

        List<View> tiles = new ArrayList<>();
        tiles.add(metric("步数", activity == null ? "—" : number(activity.opt("steps")), "步", Ui.ACCENT));
        tiles.add(metric("卡路里", activity == null ? "—" : number(activity.opt("calories")), "千卡", Ui.ACCENT));
        tiles.add(metric("睡眠", sleep == null ? "—" : duration(sleep.optInt("total_min")), "", Ui.BLUE));
        tiles.add(metric("静息心率", resting == null ? "—" : number(resting.opt("bpm")), "bpm", Ui.DANGER));

        for (int start = 0; start < tiles.size(); start += columns) {
            LinearLayout row = Ui.row(host);
            row.setGravity(Gravity.TOP);
            LinearLayout.LayoutParams rowParams = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            rowParams.bottomMargin = gutter;
            for (int column = 0; column < columns; column++) {
                LinearLayout.LayoutParams cell = new LinearLayout.LayoutParams(
                        0, ViewGroup.LayoutParams.MATCH_PARENT, 1f);
                if (column < columns - 1) cell.rightMargin = gutter;
                int position = start + column;
                if (position < tiles.size()) row.addView(tiles.get(position), cell);
                else row.addView(new View(host), cell);
            }
            grid.addView(row, rowParams);
            Ui.stagger(row, start / Math.max(1, columns));
        }

        if (workout != null && workout.optInt("count") > 0) {
            LinearLayout card = Ui.card(host);
            card.addView(Ui.cardTitle(host, "运动"));
            TextView value = Ui.text(host,
                    workout.optInt("count") + " 次 · 共 " + duration(workout.optInt("duration_s") / 60),
                    15, Ui.TEXT_2);
            value.setPadding(0, Ui.dp(host, 6), 0, 0);
            card.addView(value);
            grid.addView(card);
        }
        return grid;
    }

    private View metric(String label, String value, String unit, int color) {
        LinearLayout tile = Ui.tile(host);
        TextView name = Ui.caption(host, label);
        tile.addView(name);

        LinearLayout line = Ui.row(host);
        line.setGravity(Gravity.BOTTOM);
        line.setPadding(0, Ui.dp(host, 8), 0, 0);
        TextView number = Ui.text(host, value, 26, Ui.TEXT);
        number.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                android.graphics.Typeface.NORMAL));
        line.addView(number);
        if (!unit.isEmpty() && !"暂无".equals(value)) {
            TextView suffix = Ui.meta(host, " " + unit);
            line.addView(suffix);
        }
        tile.addView(line);

        View bar = new View(host);
        bar.setBackground(Ui.round(color, 2, host));
        LinearLayout.LayoutParams barParams = new LinearLayout.LayoutParams(
                Ui.dp(host, 24), Ui.dp(host, 3));
        barParams.topMargin = Ui.dp(host, 10);
        tile.addView(bar, barParams);
        return tile;
    }

    private View chartCard(Api.Reply series) {
        LinearLayout card = Ui.card(host);
        LinearLayout head = Ui.row(host);
        head.addView(Ui.cardTitle(host, "近 7 天步数"), new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        head.addView(Ui.meta(host, "步"));
        card.addView(head);

        TrendChart chart = new TrendChart(host);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(host, Theme.wide(host) ? 200 : 160));
        params.topMargin = Ui.dp(host, 10);
        card.addView(chart, params);

        List<Float> values = new ArrayList<>();
        List<String> labels = new ArrayList<>();
        JSONObject payload = series.json();
        if (payload != null) {
            JSONArray points = Api.array(payload, "points");
            for (int i = 0; i < points.length(); i++) {
                JSONObject point = points.optJSONObject(i);
                if (point == null) continue;
                values.add((float) point.optDouble("y", 0));
                String date = point.optString("x");
                labels.add(date.length() >= 10 ? date.substring(5) : date);
            }
        }
        float[] data = new float[values.size()];
        for (int i = 0; i < data.length; i++) data[i] = values.get(i);
        chart.setData(data, labels.toArray(new String[0]));

        if (data.length == 0) {
            TextView empty = Ui.meta(host, "这一周还没有可画的步数记录");
            empty.setPadding(0, Ui.dp(host, 6), 0, 0);
            card.addView(empty);
        }
        return card;
    }

    private View sleepCard(JSONObject sleep, String date) {
        LinearLayout card = Ui.card(host);
        LinearLayout head = Ui.row(host);
        head.addView(Ui.cardTitle(host, "睡眠结构"), new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        head.addView(Ui.meta(host, date));
        card.addView(head);

        String[][] rows = {
                {"总睡眠", duration(sleep.optInt("total_min"))},
                {"深睡", duration(sleep.optInt("deep_min"))},
                {"浅睡", duration(sleep.optInt("light_min"))},
                {"REM", duration(sleep.optInt("rem_min"))},
                {"清醒", duration(sleep.optInt("awake_min"))},
                {"睡眠得分", sleep.has("score") ? String.valueOf(sleep.optInt("score")) : "—"},
        };
        for (int index = 0; index < rows.length; index++) {
            LinearLayout line = Ui.row(host);
            line.setPadding(0, Ui.dp(host, 10), 0, 0);
            line.addView(Ui.caption(host, rows[index][0]), new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            TextView value = Ui.text(host, rows[index][1], 14, Ui.TEXT);
            value.setGravity(Gravity.END);
            line.addView(value);
            card.addView(line);
            if (index < rows.length - 1) card.addView(Ui.divider(host));
        }
        return card;
    }

    private static String day(int offsetDays) {
        Calendar calendar = Calendar.getInstance(TimeZone.getTimeZone("Asia/Shanghai"));
        calendar.add(Calendar.DAY_OF_YEAR, -offsetDays);
        return new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date(calendar.getTimeInMillis()));
    }

    private static String number(Object value) {
        if (value == null || value == JSONObject.NULL) return "暂无";
        double number = value instanceof Number ? ((Number) value).doubleValue() : parse(value.toString());
        if (number < 0) return "暂无";
        if (number == Math.rint(number)) return String.format(Locale.US, "%,.0f", number);
        return String.format(Locale.US, "%.1f", number);
    }

    private static double parse(String text) {
        try {
            return Double.parseDouble(text);
        } catch (Exception error) {
            return -1;
        }
    }

    private static String duration(int minutes) {
        if (minutes <= 0) return "—";
        int hours = minutes / 60;
        int rest = minutes % 60;
        if (hours == 0) return rest + " 分钟";
        return hours + " 小时 " + rest + " 分";
    }
}
