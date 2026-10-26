package app.odth.hub;

import android.app.Activity;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 外壳：手机用底部页签，平板（≥600dp）换成左侧导航栏，内容区按断点加宽。
 */
public class MainActivity extends Activity {

    // 对话是核心：放第一位、默认进入、图标与标题都比其他页签更醒目
    private static final int TAB_CHAT = 0;
    private static final int TAB_HOME = 1;
    private static final int TAB_HEALTH = 2;
    private static final int TAB_ME = 3;
    private static final String[] TAB_NAMES = {"对话", "首页", "健康", "我的"};
    private static final String[] TAB_ICONS = {
            GlyphView.CHAT, GlyphView.HOME, GlyphView.HEALTH, GlyphView.ME};

    private Prefs prefs;
    private ServerConfig server;
    private Api api;

    private LinearLayout root;
    private FrameLayout contentHost;
    private LinearLayout navHost;
    private TextView headerTitle;
    private TextView headerSubtitle;

    private int currentTab = TAB_CHAT;
    /** recreate() 之后要回到原来那一页，进程内传递即可。 */
    private static int restoreTab = -1;

    private HomeScreen homeScreen;
    private ChatScreen chatScreen;
    private HealthScreen healthScreen;
    private SettingsScreen meScreen;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = new Prefs(this);
        server = prefs.current();

        // 必须在建任何控件之前把调色板定下来，控件读的是 Ui 里的静态颜色
        Palette.apply(prefs.dark());
        applySystemBars();

        if (server == null) {
            showSetup(null);
        } else {
            api = new Api(server, prefs);
            showMain(restoreTab >= 0 ? restoreTab : TAB_CHAT);
            restoreTab = -1;
        }
    }

    /** 状态栏/导航栏和窗口底色跟着主题走，否则深色下会闪一下白。 */
    private void applySystemBars() {
        Window window = getWindow();
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        window.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(Ui.BG));
        window.setStatusBarColor(Ui.BG);
        window.setNavigationBarColor(Ui.SURFACE);
        if (android.os.Build.VERSION.SDK_INT >= 23) {
            // 深色主题要浅色图标，浅色主题要深色图标
            int flags = prefs.dark() ? 0 : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            if (android.os.Build.VERSION.SDK_INT >= 26 && !prefs.dark()) {
                flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            }
            window.getDecorView().setSystemUiVisibility(flags);
        }
    }

    /** 切换主题：记住当前页签，整页重建一次（各页面本来就在 refresh 里重建视图）。 */
    void applyTheme() {
        restoreTab = currentTab;
        recreate();
    }

    @Override
    public void onConfigurationChanged(android.content.res.Configuration configuration) {
        super.onConfigurationChanged(configuration);
        // 选了「跟随系统」时，系统切深色要跟着变；显式选过的就不动
        if (prefs != null && prefs.themeMode() == Prefs.THEME_SYSTEM) {
            restoreTab = currentTab;
            recreate();
        }
    }

    ServerConfig server() {
        return server;
    }

    // ------------------------------------------------------------ 主界面

    private void showMain(int tab) {
        boolean wide = Theme.wide(this);
        root = wide ? Ui.row(this) : Ui.column(this);
        root.setBackgroundColor(Ui.BG);

        LinearLayout body = Ui.column(this);
        body.addView(buildHeader(), new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        contentHost = new FrameLayout(this);
        body.addView(contentHost, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        navHost = wide ? buildRail() : buildBottomBar();

        if (wide) {
            root.addView(navHost, new LinearLayout.LayoutParams(
                    Ui.dp(this, 96), ViewGroup.LayoutParams.MATCH_PARENT));
            root.addView(body, new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.MATCH_PARENT, 1f));
        } else {
            body.addView(navHost, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            root.addView(body, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }

        setContentView(root);
        switchTab(tab);
    }

    private View buildHeader() {
        LinearLayout header = Ui.column(this);
        header.setBackgroundColor(Ui.BG);
        int padH = Ui.dp(this, Theme.gutter(this));
        header.setPadding(padH, Ui.dp(this, Theme.wide(this) ? 22 : 16), padH, Ui.dp(this, 8));

        LinearLayout line = Ui.row(this);
        LinearLayout titles = Ui.column(this);
        headerTitle = Ui.title(this, TAB_NAMES[Math.max(0, Math.min(3, currentTab))]);
        headerSubtitle = Ui.meta(this, server.label());
        headerSubtitle.setPadding(0, Ui.dp(this, 2), 0, 0);
        titles.addView(headerTitle);
        titles.addView(headerSubtitle);
        line.addView(titles, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        TextView refresh = Ui.textButton(this, "刷新", Ui.ACCENT);
        refresh.setOnClickListener(view -> refreshCurrent());
        line.addView(refresh);
        header.addView(line);
        return header;
    }

    private LinearLayout buildBottomBar() {
        LinearLayout bar = Ui.row(this);
        bar.setBackgroundColor(Ui.SURFACE);
        bar.setElevation(Ui.dp(this, 8));
        bar.setPadding(Ui.dp(this, 8), Ui.dp(this, 8), Ui.dp(this, 8), Ui.dp(this, 10));
        for (int index = 0; index < TAB_NAMES.length; index++) {
            bar.addView(navItem(TAB_NAMES[index], TAB_ICONS[index], index, false),
                    new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
        return bar;
    }

    private LinearLayout buildRail() {
        LinearLayout rail = Ui.column(this);
        rail.setBackgroundColor(Ui.SURFACE);
        rail.setPadding(Ui.dp(this, 10), Ui.dp(this, 24), Ui.dp(this, 10), Ui.dp(this, 16));
        rail.setElevation(Ui.dp(this, 2));
        for (int index = 0; index < TAB_NAMES.length; index++) {
            View item = navItem(TAB_NAMES[index], TAB_ICONS[index], index, true);
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            params.bottomMargin = Ui.dp(this, 8);
            rail.addView(item, params);
        }
        return rail;
    }

    private View navItem(String label, String icon, int index, boolean rail) {
        LinearLayout item = Ui.column(this);
        item.setGravity(Gravity.CENTER);
        int padV = Ui.dp(this, rail ? 12 : 6);
        item.setPadding(0, padV, 0, padV);
        item.setTag(index);

        boolean primary = index == TAB_CHAT;
        int iconSize = primary ? 25 : 21;
        GlyphView glyph = new GlyphView(this, icon, index == currentTab ? Ui.ACCENT
                : (primary ? Ui.TEXT_2 : Ui.TEXT_3));
        item.addView(glyph, new LinearLayout.LayoutParams(
                Ui.dp(this, iconSize), Ui.dp(this, iconSize)));

        TextView text = Ui.text(this, label, primary ? 11.5f : 10.5f,
                index == currentTab ? Ui.ACCENT : (primary ? Ui.TEXT_2 : Ui.TEXT_3));
        text.setGravity(Gravity.CENTER);
        text.setPadding(0, Ui.dp(this, 4), 0, 0);
        if (index == currentTab) {
            text.setTypeface(android.graphics.Typeface.create("sans-serif-medium",
                    android.graphics.Typeface.NORMAL));
        }
        item.addView(text);

        item.setOnClickListener(view -> switchTab(index));
        return item;
    }

    private void switchTab(int tab) {
        if (tab < 0 || tab >= TAB_NAMES.length) tab = 0;
        currentTab = tab;
        prefs.setLastTab(tab);
        paintNav();
        contentHost.removeAllViews();

        Screen screen;
        switch (tab) {
            case TAB_CHAT:
                if (chatScreen == null) chatScreen = new ChatScreen(this, api);
                screen = chatScreen;
                break;
            case TAB_HEALTH:
                if (healthScreen == null) healthScreen = new HealthScreen(this, api);
                screen = healthScreen;
                break;
            case TAB_ME:
                if (meScreen == null) meScreen = new SettingsScreen(this, prefs, api);
                screen = meScreen;
                break;
            default:
                if (homeScreen == null) homeScreen = new HomeScreen(this, api);
                screen = homeScreen;
        }
        View pane = screen.view();
        contentHost.addView(pane);
        Ui.contentIn(pane);
        boolean primary = tab == TAB_CHAT;
        headerTitle.setText(primary ? "AI 助手" : TAB_NAMES[tab]);
        headerTitle.setTextSize(primary ? 26 : 18);
        headerTitle.setTextColor(primary ? Ui.TEXT : Ui.TEXT_2);
        headerSubtitle.setText(primary ? "说一句，我来处理" : server.label());
        headerSubtitle.setVisibility(primary || Theme.wide(this) ? View.VISIBLE : View.VISIBLE);
        screen.refresh();
    }

    private void paintNav() {
        if (navHost == null) return;
        boolean rail = Theme.wide(this);
        for (int i = 0; i < navHost.getChildCount(); i++) {
            LinearLayout item = (LinearLayout) navHost.getChildAt(i);
            int index = (Integer) item.getTag();
            boolean active = index == currentTab;
            ((GlyphView) item.getChildAt(0)).setColor(active ? Ui.ACCENT : Ui.TEXT_3);
            TextView label = (TextView) item.getChildAt(1);
            label.setTextColor(active ? Ui.ACCENT : Ui.TEXT_3);
            label.setTypeface(android.graphics.Typeface.create(
                    active ? "sans-serif-medium" : "sans-serif",
                    android.graphics.Typeface.NORMAL));
            if (rail) {
                item.setBackground(active ? Ui.round(Ui.ACCENT_SOFT, 16, this) : null);
            }
            float scale = active ? 1.04f : 1f;
            item.animate().scaleX(scale).scaleY(scale).setDuration(160)
                    .setInterpolator(new android.view.animation.DecelerateInterpolator())
                    .start();
        }
    }

    private void refreshCurrent() {
        Screen screen;
        switch (currentTab) {
            case TAB_CHAT:
                screen = chatScreen;
                break;
            case TAB_HEALTH:
                screen = healthScreen;
                break;
            case TAB_ME:
                screen = meScreen;
                break;
            default:
                screen = homeScreen;
        }
        if (screen != null) screen.refresh();
    }

    int activeTabForIcon() {
        return currentTab;
    }

    // ------------------------------------------------------------ 状态回调

    void afterAssistantTurn() {
        if (homeScreen != null) homeScreen.refresh();
    }

    void onSessionChanged() {
        if (homeScreen != null) homeScreen.refresh();
        if (healthScreen != null) healthScreen.refresh();
        if (meScreen != null) meScreen.refresh();
    }

    void updateServer(ServerConfig updated) {
        this.server = updated;
        this.api = new Api(updated, prefs);
        homeScreen = null;
        chatScreen = null;
        healthScreen = null;
        meScreen = null;
        prefs.setCurrent(updated);
        if (headerSubtitle != null) headerSubtitle.setText(updated.label());
    }

    void promptLogin() {
        LinearLayout form = Ui.column(this);
        int pad = Ui.dp(this, 20);
        form.setPadding(pad, pad, pad, 0);

        EditText user = new EditText(this);
        user.setHint("管理员账号");
        user.setSingleLine(true);
        user.setText(server.username);
        user.setTextColor(Ui.TEXT);
        user.setHintTextColor(Ui.TEXT_3);
        form.addView(user);

        EditText pass = new EditText(this);
        pass.setHint("密码");
        pass.setSingleLine(true);
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        pass.setTextColor(Ui.TEXT);
        pass.setHintTextColor(Ui.TEXT_3);
        form.addView(pass);

        Ui.dialog(this)
                .setTitle("登录面板")
                .setView(form)
                .setNegativeButton("取消", null)
                .setPositiveButton("登录", (dialog, which) -> {
                    String username = user.getText().toString().trim();
                    String password = pass.getText().toString();
                    if (username.isEmpty() || password.isEmpty()) {
                        android.widget.Toast.makeText(this, "账号和密码都要填",
                                android.widget.Toast.LENGTH_SHORT).show();
                        return;
                    }
                    ServerConfig updated = new ServerConfig(server.scheme, server.host,
                            server.panelPort, server.healthPort, server.note, username, password);
                    prefs.save(updated);
                    updateServer(updated);
                    new Thread(() -> {
                        Api.Reply reply = api.login(updated.username, updated.password);
                        runOnUiThread(() -> {
                            android.widget.Toast.makeText(this,
                                    reply.ok() ? "登录成功" : "登录失败：" + reply.message(),
                                    android.widget.Toast.LENGTH_LONG).show();
                            if (reply.ok()) onSessionChanged();
                        });
                    }).start();
                })
                .show();
    }

    // ------------------------------------------------------------ 配置页

    void showSetup(ServerConfig preset) {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Ui.BG);

        LinearLayout outer = Ui.column(this);
        int gutter = Theme.wide(this) ? 32 : 20;
        outer.setPadding(Ui.dp(this, gutter), Ui.dp(this, Theme.wide(this) ? 48 : 36),
                Ui.dp(this, gutter), Ui.dp(this, 28));

        LinearLayout box = Ui.column(this);

        TextView brand = Ui.title(this, "家庭中枢");
        brand.setTextSize(28);
        box.addView(brand);
        TextView tip = Ui.text(this,
                "连接你自己的中枢。填 IP 或域名即可，App 不内置任何地址。", 14, Ui.TEXT_2);
        tip.setPadding(0, Ui.dp(this, 8), 0, Ui.dp(this, 22));
        box.addView(tip);

        LinearLayout card = Ui.card(this);
        card.addView(Ui.section(this, "服务器"));
        EditText address = input(preset != null ? preset.host : "",
                "192.168.1.10 或 hub.example.com");
        address.setInputType(InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_CLASS_TEXT);
        card.addView(address);

        LinearLayout ports = Ui.row(this);
        LinearLayout panelBox = Ui.column(this);
        panelBox.addView(Ui.meta(this, "面板端口"));
        EditText panelPort = input(preset != null ? String.valueOf(preset.panelPort) : "18765", "18765");
        panelPort.setInputType(InputType.TYPE_CLASS_NUMBER);
        panelBox.addView(panelPort);
        LinearLayout healthBox = Ui.column(this);
        healthBox.addView(Ui.meta(this, "健康端口"));
        EditText healthPort = input(preset != null ? String.valueOf(preset.healthPort) : "8321", "8321");
        healthPort.setInputType(InputType.TYPE_CLASS_NUMBER);
        healthBox.addView(healthPort);
        ports.addView(panelBox, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        ports.addView(new View(this), new LinearLayout.LayoutParams(Ui.dp(this, 12),
                ViewGroup.LayoutParams.WRAP_CONTENT));
        ports.addView(healthBox, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        card.addView(Ui.space(this, 10));
        card.addView(ports);

        card.addView(Ui.space(this, 12));
        final String[] scheme = {preset != null ? preset.scheme : "http"};
        TextView schemeButton = Ui.ghostButton(this, "协议：" + scheme[0]);
        schemeButton.setOnClickListener(view -> {
            scheme[0] = "http".equals(scheme[0]) ? "https" : "http";
            schemeButton.setText("协议：" + scheme[0]);
        });
        card.addView(schemeButton);

        card.addView(Ui.divider(this));
        card.addView(Ui.section(this, "面板账号（开了登录才需要填）"));
        EditText userField = input(preset != null ? preset.username : "", "用户名");
        card.addView(userField);
        EditText passField = input(preset != null ? preset.password : "", "密码");
        passField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        card.addView(passField);
        TextView hint = Ui.meta(this, "密码用 Android Keystore 加密后存本机，不会发往别处。");
        hint.setPadding(0, Ui.dp(this, 8), 0, 0);
        card.addView(hint);
        box.addView(card);

        TextView status = Ui.text(this, "", 13, Ui.TEXT_2);
        status.setPadding(Ui.dp(this, 4), Ui.dp(this, 4), 0, Ui.dp(this, 10));
        box.addView(status);

        TextView test = Ui.ghostButton(this, "测试连接");
        TextView save = Ui.primaryButton(this, "保存并进入");
        box.addView(test);
        box.addView(Ui.space(this, 10));
        box.addView(save);

        if (server != null) {
            TextView back = Ui.textButton(this, "返回", Ui.TEXT_2);
            back.setOnClickListener(view -> showMain(currentTab));
            box.addView(Ui.space(this, 4));
            box.addView(back);
        }

        if (Theme.wide(this)) {
            LinearLayout wrapper = Ui.row(this);
            wrapper.setGravity(Gravity.CENTER_HORIZONTAL);
            wrapper.addView(box, new LinearLayout.LayoutParams(
                    Ui.dp(this, 560), ViewGroup.LayoutParams.WRAP_CONTENT));
            outer.addView(wrapper);
        } else {
            outer.addView(box);
        }

        test.setOnClickListener(view -> {
            ServerConfig candidate = readConfig(address, panelPort, healthPort,
                    userField, passField, scheme[0], status);
            if (candidate == null) return;
            status.setTextColor(Ui.TEXT_2);
            status.setText("正在连接 " + candidate.panelBase() + " …");
            new Thread(() -> {
                Api probe = new Api(candidate, prefs);
                Api.Reply alive = probe.publicStatus();
                String note;
                boolean warn = false;
                if (!alive.ok()) {
                    note = "连不上：" + alive.message();
                } else {
                    Api.Reply state = probe.home();
                    boolean needsLogin = state.status == 401;
                    if (needsLogin && candidate.hasLogin()) {
                        Api.Reply login = probe.login(candidate.username, candidate.password);
                        note = login.ok() ? "已连接并登录" : "能连上，登录失败：" + login.message();
                        warn = !login.ok();
                    } else if (needsLogin) {
                        note = "能连上，面板已开登录；填账号密码可直接进";
                        warn = true;
                    } else {
                        note = "已连接";
                    }
                    note += " · " + alive.message();
                }
                final String summary = note;
                final boolean warning = warn;
                final boolean reachable = alive.ok();
                runOnUiThread(() -> {
                    if (isFinishing()) return;
                    status.setTextColor(warning ? Ui.WARN : (reachable ? Ui.ACCENT : Ui.DANGER));
                    status.setText(summary);
                });
            }).start();
        });

        save.setOnClickListener(view -> {
            ServerConfig candidate = readConfig(address, panelPort, healthPort,
                    userField, passField, scheme[0], status);
            if (candidate == null) return;
            prefs.save(candidate);
            prefs.setCurrent(candidate);
            server = candidate;
            api = new Api(candidate, prefs);
            homeScreen = null;
            chatScreen = null;
            healthScreen = null;
            meScreen = null;
            showMain(TAB_CHAT);
            if (candidate.hasLogin()) {
                new Thread(() -> {
                    Api.Reply login = api.login(candidate.username, candidate.password);
                    runOnUiThread(() -> {
                        if (login.ok()) onSessionChanged();
                    });
                }).start();
            }
        });

        scroll.addView(outer);
        setContentView(scroll);
    }

    private ServerConfig readConfig(EditText address, EditText panelPort, EditText healthPort,
                                    EditText userField, EditText passField,
                                    String scheme, TextView status) {
        String raw = address.getText().toString().trim();
        if (raw.isEmpty()) {
            status.setTextColor(Ui.DANGER);
            status.setText("请先填写服务器地址");
            return null;
        }
        ServerConfig base = ServerConfig.parse(raw, 0, "");
        if (base == null) {
            status.setTextColor(Ui.DANGER);
            status.setText("地址格式无法识别，试试 192.168.1.10 或 hub.example.com");
            return null;
        }
        boolean explicitPort =
                raw.replaceFirst("(?i)^https?://", "").matches("^\\[[^]]+]:\\d+.*$")
                        || raw.replaceFirst("(?i)^https?://", "").matches("^[^/:]+:\\d+.*$");
        int health = parsePort(healthPort.getText().toString(), ServerConfig.DEFAULT_HEALTH_PORT);
        int panel = explicitPort
                ? base.panelPort
                : parsePort(panelPort.getText().toString(), ServerConfig.DEFAULT_PANEL_PORT);
        return new ServerConfig(scheme, base.host, panel, health, "",
                userField.getText().toString().trim(), passField.getText().toString());
    }

    private static int parsePort(String text, int fallback) {
        try {
            int value = Integer.parseInt(text.trim());
            return value > 0 && value < 65536 ? value : fallback;
        } catch (Exception error) {
            return fallback;
        }
    }

    private EditText input(String value, String hint) {
        EditText field = new EditText(this);
        field.setText(value);
        field.setHint(hint);
        field.setTextColor(Ui.TEXT);
        field.setHintTextColor(Ui.TEXT_3);
        field.setTextSize(15);
        field.setSingleLine(true);
        field.setBackground(Ui.stroke(Ui.SURFACE_2, Ui.OUTLINE, 12, this));
        int padH = Ui.dp(this, 14);
        field.setPadding(padH, Ui.dp(this, 12), padH, Ui.dp(this, 12));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = Ui.dp(this, 8);
        field.setLayoutParams(params);
        return field;
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && currentTab != TAB_HOME && contentHost != null) {
            switchTab(TAB_HOME);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
