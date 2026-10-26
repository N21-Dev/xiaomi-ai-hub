package app.odth.hub;

import android.app.AlertDialog;
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
import java.util.List;

/** 我的：服务器、账号、模型、关于。宽屏排成两列。 */
final class SettingsScreen implements Screen {

    private final MainActivity host;
    private final Prefs prefs;
    private final Api api;
    private final ScrollView scroll;
    private final LinearLayout list;

    private LinearLayout modelCard;
    private boolean modelLoading;

    // 应用内更新
    private TextView updateButton;
    private TextView updateStatusView;
    private Updater.Latest latest;
    private String updateStatus = "";
    private boolean checking;
    private boolean downloading;
    private int updateProgress;

    SettingsScreen(MainActivity host, Prefs prefs, Api api) {
        this.host = host;
        this.prefs = prefs;
        this.api = api;

        scroll = new ScrollView(host);
        scroll.setBackgroundColor(Ui.BG);
        list = Ui.column(host);
        int gutter = Ui.dp(host, Theme.gutter(host));
        list.setPadding(gutter, Ui.dp(host, 4), gutter, Ui.dp(host, 24));
        scroll.addView(list);
    }

    @Override
    public View view() {
        return scroll;
    }

    @Override
    public void refresh() {
        list.removeAllViews();
        modelCard = null;
        modelLoading = false;
        updateButton = null;
        updateStatusView = null;

        ServerConfig server = host.server();
        boolean wide = Theme.wide(host);
        int gutter = Ui.dp(host, 12);

        List<View> cards = new ArrayList<>();
        cards.add(serverCard(server));
        cards.add(accountCard());
        modelCard = modelPlaceholder();
        cards.add(modelCard);
        cards.add(appearanceCard());
        cards.add(aboutCard());

        if (wide) {
            // 两列瀑布：左 服务器+账号，右 模型+关于
            LinearLayout columns = Ui.row(host);
            columns.setGravity(Gravity.TOP);
            LinearLayout left = Ui.column(host);
            LinearLayout right = Ui.column(host);
            left.addView(cards.get(0));
            left.addView(cards.get(1));
            right.addView(cards.get(2));
            right.addView(cards.get(3));
            right.addView(cards.get(4));
            LinearLayout.LayoutParams leftParams = new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            leftParams.rightMargin = gutter;
            columns.addView(left, leftParams);
            columns.addView(right, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            list.addView(columns);
            Ui.enter(columns, 0);
        } else {
            for (int i = 0; i < cards.size(); i++) {
                list.addView(cards.get(i));
                Ui.stagger(cards.get(i), i);
            }
        }

        loadModel();
    }

    // ------------------------------------------------------------ 卡片

    private View serverCard(ServerConfig server) {
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "服务器"));
        TextView address = Ui.text(host, server.panelBase(), 17, Ui.TEXT);
        address.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                android.graphics.Typeface.NORMAL));
        address.setPadding(0, Ui.dp(host, 2), 0, Ui.dp(host, 6));
        card.addView(address);
        card.addView(Ui.meta(host, "健康服务  " + server.healthBase()));

        TextView edit = Ui.ghostButton(host, "修改地址 / 端口");
        edit.setOnClickListener(view -> host.showSetup(server));
        card.addView(Ui.space(host, 12));
        card.addView(edit);
        return card;
    }

    private View accountCard() {
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "面板账号"));

        boolean loggedIn = api.hasSession();
        LinearLayout stateRow = Ui.row(host);
        stateRow.setPadding(0, Ui.dp(host, 2), 0, Ui.dp(host, 10));
        stateRow.addView(Ui.chip(host, loggedIn ? "已登录" : "未登录",
                loggedIn ? Ui.ACCENT : Ui.WARN,
                loggedIn ? Ui.ACCENT_SOFT : Ui.WARN_SOFT));
        TextView detail = Ui.caption(host, loggedIn
                ? "   会话 7 天有效" : "   设备与对话需要登录");
        stateRow.addView(detail);
        card.addView(stateRow);

        TextView action = loggedIn ? Ui.ghostButton(host, "退出登录") : Ui.primaryButton(host, "登录面板");
        action.setOnClickListener(view -> {
            if (api.hasSession()) {
                api.logout();
                Toast.makeText(host, "已退出登录", Toast.LENGTH_SHORT).show();
                host.onSessionChanged();
                refresh();
            } else {
                host.promptLogin();
            }
        });
        card.addView(action);
        return card;
    }

    private LinearLayout modelPlaceholder() {
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "当前模型"));
        card.addView(Ui.text(host, "读取中…", 15, Ui.TEXT_2));
        return card;
    }

    private View appearanceCard() {
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "外观"));

        LinearLayout row = Ui.row(host);
        String[] labels = {"跟随系统", "浅色", "深色"};
        int current = prefs.themeMode();
        for (int mode = 0; mode < labels.length; mode++) {
            boolean active = mode == current;
            TextView chip = Ui.text(host, labels[mode], 13.5f,
                    active ? Ui.ACCENT : Ui.TEXT_2);
            chip.setGravity(Gravity.CENTER);
            chip.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                    android.graphics.Typeface.NORMAL));
            chip.setBackground(Ui.round(active ? Ui.ACCENT_SOFT : Ui.SURFACE_2, 999, host));
            int padH = Ui.dp(host, 14);
            int padV = Ui.dp(host, 9);
            chip.setPadding(padH, padV, padH, padV);
            chip.setClickable(true);
            chip.setMinHeight(Ui.dp(host, 48));
            Ui.pressable(chip);
            final int picked = mode;
            chip.setOnClickListener(view -> {
                prefs.setThemeMode(picked);
                host.applyTheme();
            });
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            if (mode > 0) params.leftMargin = Ui.dp(host, 8);
            row.addView(chip, params);
        }
        card.addView(row);

        TextView note = Ui.meta(host, "深色模式用于夜间；选「跟随系统」时随手机设置切换。");
        note.setPadding(0, Ui.dp(host, 10), 0, 0);
        card.addView(note);
        return card;
    }

    private View aboutCard() {
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "关于"));

        LinearLayout head = Ui.row(host);
        TextView name = Ui.text(host, "家庭中枢 Android 客户端", 15, Ui.TEXT);
        name.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                android.graphics.Typeface.NORMAL));
        head.addView(name, Ui.weight(1f));
        head.addView(Ui.text(host, "v" + Updater.versionName(host), 13, Ui.TEXT_3));
        card.addView(head);

        updateStatusView = Ui.meta(host, updateStatus);
        updateStatusView.setPadding(0, Ui.dp(host, 6), 0, Ui.dp(host, 10));
        card.addView(updateStatusView);

        updateButton = Ui.primaryButton(host, "检查更新");
        updateButton.setOnClickListener(view -> onUpdateClick());
        card.addView(updateButton);

        TextView note = Ui.meta(host, "更新包由中枢直接提供，不走应用商店。下载后会校验内容与签名，"
                + "再交给系统安装器；装不装由你决定。");
        note.setPadding(0, Ui.dp(host, 10), 0, 0);
        card.addView(note);

        applyUpdateState();
        return card;
    }

    // ------------------------------------------------------------ 更新

    private void onUpdateClick() {
        if (checking || downloading) return;
        if (latest != null) {
            startUpdate();
            return;
        }
        checkRelease();
    }

    private void checkRelease() {
        checking = true;
        updateStatus = "";
        applyUpdateState();
        new Thread(() -> {
            Updater.Latest found = null;
            String failure = null;
            try {
                found = Updater.check(host.server());
            } catch (Exception error) {
                failure = error.getMessage();
            }
            final Updater.Latest release = found;
            final String error = failure;
            host.runOnUiThread(() -> {
                checking = false;
                if (error != null) {
                    latest = null;
                    updateStatus = "检查失败：" + error;
                } else if (release == null) {
                    latest = null;
                    updateStatus = "服务器上还没有发布安装包";
                } else if (release.versionCode <= Updater.versionCode(host)) {
                    latest = null;
                    updateStatus = "已是最新版本（v" + release.versionName + "）";
                } else {
                    latest = release;
                    updateStatus = "新版本 v" + release.versionName
                            + (release.notes.isEmpty() ? "" : " · " + release.notes);
                }
                applyUpdateState();
            });
        }).start();
    }

    private void startUpdate() {
        if (latest == null) return;
        if (!Updater.canInstall(host)) {
            updateStatus = "请在系统设置里允许本应用安装未知应用，返回后再点一次";
            applyUpdateState();
            Updater.requestInstallPermission(host);
            return;
        }
        downloading = true;
        updateProgress = 0;
        applyUpdateState();
        new Thread(() -> {
            try {
                final java.io.File apk = Updater.download(host, host.server(), latest,
                        percent -> host.runOnUiThread(() -> {
                            updateProgress = percent;
                            applyUpdateState();
                        }));
                host.runOnUiThread(() -> {
                    downloading = false;
                    updateStatus = "下载完成，正在打开系统安装器";
                    applyUpdateState();
                    Updater.install(host, apk);
                });
            } catch (Exception error) {
                host.runOnUiThread(() -> {
                    downloading = false;
                    updateStatus = "更新失败：" + error.getMessage();
                    applyUpdateState();
                });
            }
        }).start();
    }

    private void applyUpdateState() {
        if (updateButton == null || updateStatusView == null) return;
        String label;
        boolean enabled = true;
        if (downloading) {
            label = "下载中 " + updateProgress + "%";
            enabled = false;
        } else if (checking) {
            label = "检查中…";
            enabled = false;
        } else if (latest != null) {
            label = "一键更新到 v" + latest.versionName;
        } else {
            label = "检查更新";
        }
        updateButton.setText(label);
        updateButton.setEnabled(enabled);
        updateButton.setAlpha(enabled ? 1f : 0.5f);
        updateStatusView.setText(updateStatus);
        updateStatusView.setVisibility(updateStatus.isEmpty()
                ? View.GONE : View.VISIBLE);
        updateStatusView.setTextColor(latest != null && !downloading ? Ui.ACCENT : Ui.TEXT_3);
    }

    // ------------------------------------------------------------ 模型

    private void loadModel() {
        if (modelLoading || modelCard == null) return;
        modelLoading = true;
        new Thread(() -> {
            Api.Reply reply = api.models();
            host.runOnUiThread(() -> {
                modelLoading = false;
                renderModel(reply);
            });
        }).start();
    }

    private void renderModel(Api.Reply reply) {
        if (host.isFinishing() || modelCard == null) return;
        int index = list.indexOfChild(modelCard);
        boolean nested = index < 0;
        LinearLayout card = Ui.card(host);
        card.addView(Ui.section(host, "当前模型"));

        if (!reply.ok()) {
            card.addView(Ui.text(host,
                    reply.status == 401 ? "登录后可查看并切换模型" : "读取失败：" + reply.message(),
                    14, reply.status == 401 ? Ui.WARN : Ui.DANGER));
            replaceModelCard(card);
            return;
        }

        JSONObject payload = reply.json();
        JSONObject active = payload == null ? null : payload.optJSONObject("active");
        String providerId = active == null ? "" : active.optString("provider_id");
        String model = active == null ? "" : active.optString("model");

        TextView current = Ui.text(host, model.isEmpty() ? "未设置" : model, 18, Ui.TEXT);
        current.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                android.graphics.Typeface.NORMAL));
        current.setPadding(0, Ui.dp(host, 2), 0, Ui.dp(host, 2));
        card.addView(current);
        if (!providerId.isEmpty()) card.addView(Ui.meta(host, "渠道 " + providerId));

        JSONArray fallback = Api.array(payload, "fallback");
        if (fallback.length() > 0) {
            StringBuilder chain = new StringBuilder();
            for (int i = 0; i < fallback.length(); i++) {
                JSONObject item = fallback.optJSONObject(i);
                if (item == null) continue;
                if (chain.length() > 0) chain.append("  →  ");
                chain.append(item.optString("model"));
            }
            TextView line = Ui.meta(host, "备用：" + chain);
            line.setPadding(0, Ui.dp(host, 8), 0, 0);
            card.addView(line);
        }

        TextView switchModel = Ui.ghostButton(host, "切换模型");
        switchModel.setOnClickListener(view -> chooseModel(payload));
        card.addView(Ui.space(host, 12));
        card.addView(switchModel);
        replaceModelCard(card);
    }

    private void replaceModelCard(LinearLayout card) {
        LinearLayout parent = (LinearLayout) modelCard.getParent();
        if (parent == null) return;
        int position = parent.indexOfChild(modelCard);
        parent.removeViewAt(position);
        parent.addView(card, position);
        modelCard = card;
    }

    private void chooseModel(JSONObject registry) {
        JSONArray providers = Api.array(registry, "providers");
        List<String> labels = new ArrayList<>();
        List<String[]> targets = new ArrayList<>();
        for (int i = 0; i < providers.length(); i++) {
            JSONObject provider = providers.optJSONObject(i);
            if (provider == null || !provider.optBoolean("enabled", true)) continue;
            JSONArray models = Api.array(provider, "models");
            for (int j = 0; j < models.length(); j++) {
                Object entry = models.opt(j);
                String name = entry instanceof JSONObject
                        ? ((JSONObject) entry).optString("name") : String.valueOf(entry);
                if (name.isEmpty()) continue;
                labels.add(provider.optString("name", provider.optString("id")) + " · " + name);
                targets.add(new String[]{provider.optString("id"), name});
            }
        }
        if (labels.isEmpty()) {
            Toast.makeText(host, "没有可切换的模型", Toast.LENGTH_SHORT).show();
            return;
        }
        int revision = registry.optInt("revision", 0);
        Ui.dialog(host)
                .setTitle("选择模型（" + labels.size() + "）")
                .setItems(labels.toArray(new String[0]), (dialog, which) -> {
                    String[] target = targets.get(which);
                    switchTo(target[0], target[1], revision);
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void switchTo(String providerId, String model, int revision) {
        new Thread(() -> {
            Api.Reply reply = api.activate(providerId, model, revision);
            host.runOnUiThread(() -> {
                if (reply.ok()) {
                    Toast.makeText(host, "已切换到 " + model, Toast.LENGTH_SHORT).show();
                    loadModel();
                } else {
                    Toast.makeText(host, "切换失败：" + reply.message(), Toast.LENGTH_LONG).show();
                }
            });
        }).start();
    }
}
