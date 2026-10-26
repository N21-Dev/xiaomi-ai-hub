package app.odth.hub;

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.View;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.animation.DecelerateInterpolator;

/**
 * 米家式开关：灰/绿药丸轨道 + 白色滑块，点按有位移动画。
 *
 * 视觉尺寸固定 44×26dp，但**控件占位 48×48dp**（Material 的最小触摸目标）。
 * 轨道在控件里居中绘制，所以外观不变、手指好按。
 */
final class SwitchView extends View {

    private static final float TRACK_WIDTH_DP = 44f;
    private static final float TRACK_HEIGHT_DP = 26f;
    private static final float TOUCH_SIZE_DP = 48f;

    private final Paint trackPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint thumbPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint thumbRing = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF rect = new RectF();
    private boolean checked;
    private float position;   // 0 = 关，1 = 开

    SwitchView(Context context) {
        super(context);
        setClickable(true);
        thumbRing.setStyle(Paint.Style.STROKE);
        setFocusable(true);
    }

    void setChecked(boolean value, boolean animate) {
        if (checked == value && animate) return;
        checked = value;
        if (animate) {
            ValueAnimator animator = ValueAnimator.ofFloat(position, value ? 1f : 0f);
            animator.setDuration(160);
            animator.setInterpolator(new DecelerateInterpolator());
            animator.addUpdateListener(animation -> {
                position = (float) animation.getAnimatedValue();
                invalidate();
            });
            animator.start();
        } else {
            position = value ? 1f : 0f;
            invalidate();
        }
    }

    boolean isChecked() {
        return checked;
    }

    @Override
    protected void onDraw(Canvas canvas) {
        Context context = getContext();
        float trackWidth = Math.min(Ui.dp(context, TRACK_WIDTH_DP), getWidth());
        float trackHeight = Math.min(Ui.dp(context, TRACK_HEIGHT_DP), getHeight());
        float left = (getWidth() - trackWidth) / 2f;
        float top = (getHeight() - trackHeight) / 2f;
        float radius = trackHeight / 2f;

        trackPaint.setColor(Ui.blend(Ui.SWITCH_OFF, Ui.ACCENT, position));
        rect.set(left, top, left + trackWidth, top + trackHeight);
        canvas.drawRoundRect(rect, radius, radius, trackPaint);

        float inset = trackHeight * 0.12f;
        float thumbRadius = radius - inset;
        float centerX = left + inset + thumbRadius
                + (trackWidth - 2 * (inset + thumbRadius)) * position;
        float centerY = getHeight() / 2f;

        thumbPaint.setColor(Ui.SURFACE);
        canvas.drawCircle(centerX, centerY, thumbRadius, thumbPaint);
        // 关闭态是白滑块压在浅灰轨道上，只有 1.2:1，加一圈描边才看得出边界
        if (position < 0.99f) {
            thumbRing.setColor(Ui.OUTLINE);
            thumbRing.setAlpha((int) (255 * (1f - position)));
            thumbRing.setStrokeWidth(Ui.dp(context, 1));
            canvas.drawCircle(centerX, centerY, thumbRadius, thumbRing);
        }
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int size = Ui.dp(getContext(), TOUCH_SIZE_DP);
        setMeasuredDimension(size, size);
    }

    /** 让读屏把这个自绘控件当开关播报，而不是一个普通按钮。 */
    @Override
    public void onInitializeAccessibilityNodeInfo(AccessibilityNodeInfo info) {
        super.onInitializeAccessibilityNodeInfo(info);
        info.setClassName("android.widget.Switch");
        info.setCheckable(true);
        info.setChecked(checked);
    }
}
