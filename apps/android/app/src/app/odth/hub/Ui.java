package app.odth.hub;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 界面基础件：卡片、按钮、文字、开关，以及一套可切换的颜色令牌。
 * 间距走 4dp 栅格，圆角只用 8/14/20/全圆四档，保证页面之间一致。
 *
 * 颜色字段是 **可变静态量**：{@link Palette#apply(boolean)} 切换浅色/深色时整组重新赋值，
 * 所以这里不要写成 final，也不要在控件里缓存颜色值（绘制时现取）。
 */
final class Ui {

    static int BG = 0xFFF5F6F8;
    static int SURFACE = 0xFFFFFFFF;
    static int SURFACE_2 = 0xFFF2F4F7;
    static int DIVIDER = 0xFFEDEFF3;
    static int OUTLINE = 0xFFE6E9EF;

    static int TEXT = 0xFF1B1E23;
    static int TEXT_2 = 0xFF6B7280;
    static int TEXT_3 = 0xFF9AA1AC;

    static int ACCENT = 0xFF00B389;
    static int ACCENT_SOFT = 0xFFE6F7F2;
    static int ACCENT_TEXT = 0xFFFFFFFF;
    static int WARN = 0xFFF59E0B;
    static int WARN_SOFT = 0xFFFDF3E3;
    static int DANGER = 0xFFE5484D;
    static int BLUE = 0xFF3B82F6;
    static int SWITCH_OFF = 0xFFE3E6EB;
    static int CODE_BG = 0xFFEEF1E9;

    private Ui() {
    }

    /**
     * 系统对话框不带 AppCompat，浅色/深色要各指定一个 framework 主题，
     * 否则深色模式下会弹出一张白底弹窗。
     */
    static android.app.AlertDialog.Builder dialog(Context context) {
        return new android.app.AlertDialog.Builder(context,
                Palette.isDark()
                        ? android.R.style.Theme_Material_Dialog_Alert
                        : android.R.style.Theme_Material_Light_Dialog_Alert);
    }

    static int dp(Context context, float value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }

    // ------------------------------------------------------------ 形状

    static GradientDrawable round(int color, float radiusDp, Context context) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(context, radiusDp));
        return drawable;
    }

    static GradientDrawable pill(int color, Context context) {
        return round(color, 999, context);
    }

    static GradientDrawable stroke(int fill, int strokeColor, float radiusDp, Context context) {
        GradientDrawable drawable = round(fill, radiusDp, context);
        drawable.setStroke(dp(context, 1), strokeColor);
        return drawable;
    }

    /** 卡片：白底、20dp 圆角、极浅描边。层次靠留白，不靠重阴影。 */
    static LinearLayout card(Context context) {
        LinearLayout card = column(context);
        card.setBackground(stroke(SURFACE, OUTLINE, 20, context));
        card.setElevation(dp(context, 1));
        int pad = dp(context, 16);
        card.setPadding(pad, pad, pad, pad);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.bottomMargin = dp(context, 12);
        card.setLayoutParams(params);
        return card;
    }

    /** 无底部间距的卡片，用于自己控制网格间距的场景。 */
    static LinearLayout tile(Context context) {
        LinearLayout card = card(context);
        ((LinearLayout.LayoutParams) card.getLayoutParams()).bottomMargin = 0;
        return card;
    }

    static LinearLayout column(Context context) {
        LinearLayout layout = new LinearLayout(context);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    static LinearLayout row(Context context) {
        LinearLayout layout = new LinearLayout(context);
        layout.setOrientation(LinearLayout.HORIZONTAL);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        return layout;
    }

    // ------------------------------------------------------------ 文字

    static TextView text(Context context, String value, float sizeSp, int color) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(sizeSp);
        view.setTextColor(color);
        view.setLineSpacing(dp(context, 3), 1f);
        return view;
    }

    static TextView title(Context context, String value) {
        TextView view = text(context, value, 24, TEXT);
        view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return view;
    }

    static TextView cardTitle(Context context, String value) {
        TextView view = text(context, value, 15.5f, TEXT);
        view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return view;
    }

    static TextView section(Context context, String value) {
        TextView view = text(context, value, 12.5f, TEXT_3);
        view.setLetterSpacing(0.04f);
        view.setPadding(dp(context, 4), dp(context, 8), 0, dp(context, 8));
        return view;
    }

    static TextView caption(Context context, String value) {
        return text(context, value, 12, TEXT_2);
    }

    static TextView meta(Context context, String value) {
        return text(context, value, 12, TEXT_3);
    }

    // ------------------------------------------------------------ 控件

    static TextView primaryButton(Context context, String label) {
        return button(context, label, ACCENT, ACCENT_TEXT, 0x33000000);
    }

    static TextView ghostButton(Context context, String label) {
        return button(context, label, SURFACE_2, TEXT, 0x14000000);
    }

    static TextView textButton(Context context, String label, int color) {
        TextView view = text(context, label, 14, color);
        view.setGravity(Gravity.CENTER);
        view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        int padH = dp(context, 12);
        int padV = dp(context, 8);
        view.setPadding(padH, padV, padH, padV);
        view.setMinHeight(dp(context, 48));   // 触摸目标下限
        view.setClickable(true);
        view.setFocusable(true);
        return view;
    }

    private static TextView button(Context context, String label, int fill, int textColor, int rippleColor) {
        TextView view = text(context, label, 15, textColor);
        view.setGravity(Gravity.CENTER);
        view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        view.setBackground(new RippleDrawable(ColorStateList.valueOf(rippleColor),
                round(fill, 14, context), null));
        int padV = dp(context, 13);
        int padH = dp(context, 16);
        view.setPadding(padH, padV, padH, padV);
        view.setMinHeight(dp(context, 48));   // 触摸目标下限
        view.setClickable(true);
        view.setFocusable(true);
        return view;
    }

    static TextView chip(Context context, String label, int textColor, int fill) {
        TextView view = text(context, label, 11.5f, textColor);
        view.setGravity(Gravity.CENTER);
        view.setPadding(dp(context, 8), dp(context, 3), dp(context, 8), dp(context, 3));
        view.setBackground(pill(fill, context));
        return view;
    }

    static View divider(Context context) {
        View view = new View(context);
        view.setBackgroundColor(DIVIDER);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Math.max(1, dp(context, 0.7f)));
        params.topMargin = dp(context, 10);
        params.bottomMargin = dp(context, 10);
        view.setLayoutParams(params);
        return view;
    }

    static View space(Context context, int heightDp) {
        View view = new View(context);
        view.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(context, heightDp)));
        return view;
    }

    // ------------------------------------------------------------ 动画

    /** 进入动画：淡入 + 上移 10dp。只用 alpha/translation，走 GPU 合成，不触发布局。 */
    static void enter(View view, int delayMs) {
        view.setAlpha(0f);
        view.setTranslationY(dp(view.getContext(), 10));
        view.animate()
                .alpha(1f)
                .translationY(0f)
                .setStartDelay(delayMs)
                .setDuration(200)
                .setInterpolator(new android.view.animation.DecelerateInterpolator(1.6f))
                .start();
    }

    /** 列表逐项入场：每项延后 26ms，超过 8 项就不再叠加，避免长列表等太久。 */
    static void stagger(View view, int index) {
        enter(view, Math.min(index, 8) * 26);
    }

    /** 页签切换的内容过渡：非常轻的缩放 + 淡入。 */
    static void contentIn(View view) {
        view.setAlpha(0f);
        view.setScaleX(0.992f);
        view.setScaleY(0.992f);
        view.animate()
                .alpha(1f)
                .scaleX(1f)
                .scaleY(1f)
                .setDuration(180)
                .setInterpolator(new android.view.animation.DecelerateInterpolator(1.4f))
                .start();
    }

    /** 按压反馈：按下缩到 0.97，抬起回弹。给所有可点卡片/按钮用。 */
    static void pressable(View view) {
        view.setOnTouchListener((target, event) -> {
            switch (event.getActionMasked()) {
                case android.view.MotionEvent.ACTION_DOWN:
                    target.animate().scaleX(0.97f).scaleY(0.97f).setDuration(90).start();
                    break;
                case android.view.MotionEvent.ACTION_UP:
                case android.view.MotionEvent.ACTION_CANCEL:
                    target.animate().scaleX(1f).scaleY(1f).setDuration(140)
                            .setInterpolator(new android.view.animation.OvershootInterpolator(2.2f))
                            .start();
                    break;
                default:
                    break;
            }
            return false;
        });
    }

    static LinearLayout.LayoutParams weight(float value) {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, value);
    }

    static int blend(int base, int overlay, float ratio) {
        int r = (int) (android.graphics.Color.red(base) * (1 - ratio)
                + android.graphics.Color.red(overlay) * ratio);
        int g = (int) (android.graphics.Color.green(base) * (1 - ratio)
                + android.graphics.Color.green(overlay) * ratio);
        int b = (int) (android.graphics.Color.blue(base) * (1 - ratio)
                + android.graphics.Color.blue(overlay) * ratio);
        return android.graphics.Color.rgb(r, g, b);
    }
}
