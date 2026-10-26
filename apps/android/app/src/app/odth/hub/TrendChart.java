package app.odth.hub;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.view.View;

/** 近 7 天步数的极简柱状图，纯 Canvas 绘制。 */
final class TrendChart extends View {

    private final Paint bar = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint line = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();

    private float[] values = new float[0];
    private String[] labels = new String[0];

    TrendChart(android.content.Context context) {
        super(context);
        text.setColor(Ui.TEXT_3);
        text.setTextSize(context.getResources().getDisplayMetrics().scaledDensity * 10f);
        text.setTextAlign(Paint.Align.CENTER);
        line.setColor(Ui.OUTLINE);
        line.setStrokeWidth(context.getResources().getDisplayMetrics().density);
        bar.setColor(Ui.ACCENT);
        bar.setAlpha(235);
    }

    void setData(float[] values, String[] labels) {
        this.values = values;
        this.labels = labels;
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float width = getWidth();
        float height = getHeight();
        float labelSpace = 26f * getResources().getDisplayMetrics().density;
        float chartHeight = height - labelSpace;
        canvas.drawLine(0, chartHeight, width, chartHeight, line);

        if (values.length == 0) {
            text.setColor(Ui.TEXT_3);
            canvas.drawText("暂无数据", width / 2, chartHeight / 2, text);
            return;
        }

        float max = 1f;
        for (float value : values) max = Math.max(max, value);
        float slot = width / values.length;
        float barWidth = Math.min(slot * 0.46f, 26f * getResources().getDisplayMetrics().density);

        for (int i = 0; i < values.length; i++) {
            float value = values[i];
            float ratio = Math.max(0f, Math.min(1f, value / max));
            float barHeight = Math.max(2f, ratio * (chartHeight - 12f));
            float left = i * slot + (slot - barWidth) / 2f;
            float top = chartHeight - barHeight;
            android.graphics.RectF rect = new android.graphics.RectF(
                    left, top, left + barWidth, chartHeight);
            float radius = barWidth / 2f;
            canvas.drawRoundRect(rect, radius, radius, bar);

            String label = i < labels.length ? labels[i] : "";
            if (!label.isEmpty()) {
                text.setColor(Ui.TEXT_3);
                canvas.drawText(label, i * slot + slot / 2f, height - 8f, text);
            }
        }
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int width = resolveSize(getSuggestedMinimumWidth(), widthMeasureSpec);
        int height = resolveSize(Ui.dp(getContext(), 140), heightMeasureSpec);
        setMeasuredDimension(width, height);
    }
}
