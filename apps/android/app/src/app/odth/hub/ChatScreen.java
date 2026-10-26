package app.odth.hub;

import android.graphics.Typeface;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.security.SecureRandom;
import java.util.Calendar;

/**
 * 核心页面：AI 对话。占据最突出的视觉位置，其余页签弱化。
 * 空状态给问候 + 建议词，输入区做成悬浮卡片，发送是圆形主色按钮。
 */
final class ChatScreen implements Screen {

    private static final int MAX_WIDTH_DP = 780;
    private static final String[] SUGGESTIONS = {
            "客厅的灯关一下", "家里现在什么情况", "今天走了多少步", "书房空调调到 26 度"};

    private final MainActivity host;
    private final Api api;
    private final LinearLayout root;
    private final ScrollView scroll;
    private final LinearLayout messages;
    private final LinearLayout hero;
    private EditText input;
    private final LinearLayout sendButton;
    private String conversationId = freshId();
    private boolean busy;

    ChatScreen(MainActivity host, Api api) {
        this.host = host;
        this.api = api;

        root = Ui.column(host);
        root.setBackgroundColor(Ui.BG);

        scroll = new ScrollView(host);
        messages = Ui.column(host);
        int gutter = Ui.dp(host, Theme.gutter(host));
        messages.setPadding(gutter, Ui.dp(host, 4), gutter, Ui.dp(host, 16));

        // 空状态：问候 + 建议词，视觉重心在这里
        hero = Ui.column(host);
        TextView greeting = Ui.text(host, greeting(), 30, Ui.TEXT);
        greeting.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        hero.addView(greeting);
        TextView sub = Ui.text(host, "有什么要我做的？直接说就行。", 14.5f, Ui.TEXT_2);
        sub.setPadding(0, Ui.dp(host, 8), 0, Ui.dp(host, 22));
        hero.addView(sub);

        LinearLayout chips = Ui.column(host);
        for (int row = 0; row < 2; row++) {
            LinearLayout line = Ui.row(host);
            line.setPadding(0, 0, 0, Ui.dp(host, 10));
            for (int column = 0; column < 2; column++) {
                final String text = SUGGESTIONS[row * 2 + column];
                TextView chip = Ui.text(host, text, 13.5f, Ui.TEXT_2);
                chip.setPadding(Ui.dp(host, 14), Ui.dp(host, 10), Ui.dp(host, 14), Ui.dp(host, 10));
                chip.setBackground(Ui.stroke(Ui.SURFACE, Ui.OUTLINE, 14, host));
                Ui.pressable(chip);
                chip.setOnClickListener(view -> {
                    input.setText(text);
                    submit();
                });
                LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0,
                        ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
                if (column == 0) params.rightMargin = Ui.dp(host, 10);
                line.addView(chip, params);
            }
            chips.addView(line);
        }
        hero.addView(chips);
        messages.addView(hero);

        LinearLayout center = Ui.column(host);
        center.setGravity(Gravity.CENTER_HORIZONTAL);
        if (Theme.wide(host)) {
            LinearLayout holder = Ui.column(host);
            holder.addView(messages, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            center.addView(holder, new LinearLayout.LayoutParams(
                    Ui.dp(host, MAX_WIDTH_DP), ViewGroup.LayoutParams.MATCH_PARENT));
        } else {
            center.addView(messages, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        }
        scroll.addView(center);

        // 输入区：白色浮起卡片，输入框 + 圆形发送
        LinearLayout bar = Ui.row(host);
        bar.setBackgroundColor(Ui.BG);
        bar.setPadding(Ui.dp(host, 12), Ui.dp(host, 6), Ui.dp(host, 12), Ui.dp(host, 12));

        LinearLayout field = Ui.row(host);
        field.setBackground(Ui.stroke(Ui.SURFACE, Ui.OUTLINE, 22, host));
        field.setElevation(Ui.dp(host, 3));
        field.setPadding(Ui.dp(host, 16), Ui.dp(host, 4), Ui.dp(host, 6), Ui.dp(host, 4));

        input = new EditText(host);
        input.setHint("说点什么…");
        input.setHintTextColor(Ui.TEXT_3);
        input.setTextColor(Ui.TEXT);
        input.setTextSize(15);
        input.setMaxLines(4);
        input.setBackground(null);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        input.setPadding(0, Ui.dp(host, 10), 0, Ui.dp(host, 10));
        field.addView(input, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        sendButton = Ui.column(host);
        sendButton.setGravity(Gravity.CENTER);
        sendButton.setBackground(Ui.round(Ui.ACCENT, 999, host));
        sendButton.setClickable(true);
        sendButton.setContentDescription("发送");
        GlyphView sendIcon = new GlyphView(host, GlyphView.SEND, Ui.ACCENT_TEXT);
        sendButton.addView(sendIcon, new LinearLayout.LayoutParams(
                Ui.dp(host, 19), Ui.dp(host, 19)));
        Ui.pressable(sendButton);
        sendButton.setOnClickListener(view -> submit());
        field.addView(sendButton, new LinearLayout.LayoutParams(Ui.dp(host, 42), Ui.dp(host, 42)));

        bar.addView(field, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        // 宽屏时输入条与消息区同宽同位置，避免一边满宽一边收窄
        if (Theme.wide(host)) {
            LinearLayout barHolder = Ui.row(host);
            barHolder.setGravity(Gravity.CENTER_HORIZONTAL);
            bar.setBackgroundColor(Ui.BG);
            barHolder.addView(bar, new LinearLayout.LayoutParams(
                    Ui.dp(host, MAX_WIDTH_DP), ViewGroup.LayoutParams.WRAP_CONTENT));
            root.addView(barHolder);
        } else {
            root.addView(bar);
        }
    }

    @Override
    public View view() {
        return root;
    }

    @Override
    public void refresh() {
        if (messages.getChildCount() <= 1) {
            hero.setVisibility(View.VISIBLE);
            Ui.enter(hero, 40);
        }
    }

    void clear() {
        while (messages.getChildCount() > 1) messages.removeViewAt(1);
        hero.setVisibility(View.VISIBLE);
        conversationId = freshId();
        Ui.enter(hero, 0);
    }

    private void submit() {
        String text = input.getText().toString().trim();
        if (text.isEmpty() || busy) return;
        input.setText("");
        hero.setVisibility(View.GONE);
        bubble("user", text, true);
        final String conversation = conversationId;
        setBusy(true);

        final LinearLayout typing = typingBubble();
        messages.addView(typing);
        scrollToEnd();

        new Thread(() -> {
            Api.Reply reply = api.chat(text, true, conversation);
            host.runOnUiThread(() -> {
                messages.removeView(typing);
                setBusy(false);
                if (reply.ok()) {
                    JSONObject payload = reply.json();
                    String answer = payload == null ? "" : payload.optString("answer");
                    bubble("assistant", answer.isEmpty() ? "（没有返回内容）" : answer, true);
                    host.afterAssistantTurn();
                } else if (reply.status == 401) {
                    bubble("assistant", "面板需要登录。到「我的」里登录后就能对话。", true);
                } else {
                    bubble("assistant", "请求失败：" + reply.message(), true);
                }
            });
        }).start();
    }

    private void setBusy(boolean value) {
        busy = value;
        sendButton.setEnabled(!value);
        sendButton.animate().alpha(value ? 0.45f : 1f).scaleX(value ? 0.92f : 1f)
                .scaleY(value ? 0.92f : 1f).setDuration(140).start();
    }

    private void bubble(String role, String text, boolean scrollToEnd) {
        boolean mine = "user".equals(role);
        LinearLayout row = Ui.row(host);
        row.setGravity(mine ? Gravity.END : Gravity.START);
        row.setPadding(0, Ui.dp(host, 5), 0, Ui.dp(host, 5));

        TextView bubble = Ui.text(host, text, 15, mine ? Ui.ACCENT_TEXT : Ui.TEXT);
        bubble.setLineSpacing(Ui.dp(host, 4), 1f);
        // 助手回复按 Markdown 渲染；用户自己打的字原样显示，免得一个 * 就被吃掉
        if (!mine) {
            bubble.setText(Markdown.render(text, Ui.TEXT, Ui.TEXT_2, Ui.ACCENT));
        }
        bubble.setBackground(mine
                ? Ui.round(Ui.ACCENT, 18, host)
                : Ui.stroke(Ui.SURFACE, Ui.OUTLINE, 18, host));
        int padH = Ui.dp(host, 15);
        int padV = Ui.dp(host, 11);
        bubble.setPadding(padH, padV, padH, padV);
        bubble.setMaxWidth((int) (host.getResources().getDisplayMetrics().widthPixels * 0.76f));

        row.addView(bubble);
        messages.addView(row);

        // 气泡入场：轻微缩放 + 淡入
        row.setAlpha(0f);
        row.setScaleX(0.96f);
        row.setScaleY(0.96f);
        row.animate().alpha(1f).scaleX(1f).scaleY(1f).setDuration(180)
                .setInterpolator(new android.view.animation.DecelerateInterpolator(1.6f))
                .start();

        if (scrollToEnd) scrollToEnd();
    }

    /** 等待回复时的“正在输入”三点，纯 View 画，不占布局动画开销。 */
    private LinearLayout typingBubble() {
        LinearLayout row = Ui.row(host);
        row.setGravity(Gravity.START);
        row.setPadding(0, Ui.dp(host, 5), 0, Ui.dp(host, 5));

        LinearLayout bubble = Ui.row(host);
        bubble.setBackground(Ui.stroke(Ui.SURFACE, Ui.OUTLINE, 18, host));
        bubble.setPadding(Ui.dp(host, 16), Ui.dp(host, 14), Ui.dp(host, 16), Ui.dp(host, 14));
        for (int i = 0; i < 3; i++) {
            View dot = new View(host);
            dot.setBackground(Ui.round(Ui.TEXT_3, 999, host));
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                    Ui.dp(host, 6), Ui.dp(host, 6));
            if (i > 0) params.leftMargin = Ui.dp(host, 5);
            bubble.addView(dot, params);
            dot.setAlpha(0.35f);
            dot.animate().alpha(1f).setDuration(520).setStartDelay(i * 130L)
                    .setInterpolator(new android.view.animation.AccelerateDecelerateInterpolator())
                    .withEndAction(() -> dot.animate().alpha(0.35f).setDuration(520).start())
                    .start();
        }
        row.addView(bubble);
        return row;
    }

    private void scrollToEnd() {
        scroll.post(() -> scroll.fullScroll(View.FOCUS_DOWN));
    }

    private static String greeting() {
        int hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY);
        if (hour < 6) return "还没睡？";
        if (hour < 11) return "早上好";
        if (hour < 14) return "中午好";
        if (hour < 18) return "下午好";
        return "晚上好";
    }

    private static String freshId() {
        byte[] bytes = new byte[12];
        new SecureRandom().nextBytes(bytes);
        StringBuilder out = new StringBuilder("android-");
        for (byte value : bytes) out.append(String.format("%02x", value));
        return out.toString();
    }
}
