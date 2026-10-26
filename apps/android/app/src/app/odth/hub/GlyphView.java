package app.odth.hub;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.view.View;

/**
 * 线性图标。米家的图标是细线、圆头、24dp 视觉框，这里按同样规则画。
 * 不引 drawable 资源，改名/改色只改一处。
 */
final class GlyphView extends View {

    static final String HOME = "home";
    static final String CHAT = "chat";
    static final String HEALTH = "health";
    static final String ME = "me";
    static final String BULB = "bulb";
    static final String PLUG = "plug";
    static final String AC = "ac";
    static final String SENSOR = "sensor";
    static final String ROUTER = "router";
    static final String DEVICE = "device";
    static final String SEND = "send";

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();
    private final RectF rect = new RectF();
    private String name;
    private int color;

    GlyphView(Context context, String name, int color) {
        super(context);
        this.name = name;
        this.color = color;
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(context.getResources().getDisplayMetrics().density * 1.7f);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
    }

    void setColor(int value) {
        color = value;
        invalidate();
    }

    void setName(String value) {
        name = value;
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        paint.setColor(color);
        float w = getWidth();
        float h = getHeight();
        float cx = w / 2f;
        float cy = h / 2f;
        float u = Math.min(w, h) / 2f;

        switch (name == null ? DEVICE : name) {
            case HOME:
                path.reset();
                path.moveTo(cx - u * 0.88f, cy + u * 0.06f);
                path.lineTo(cx, cy - u * 0.80f);
                path.lineTo(cx + u * 0.88f, cy + u * 0.06f);
                canvas.drawPath(path, paint);
                path.reset();
                path.moveTo(cx - u * 0.58f, cy - u * 0.06f);
                path.lineTo(cx - u * 0.58f, cy + u * 0.80f);
                path.lineTo(cx + u * 0.58f, cy + u * 0.80f);
                path.lineTo(cx + u * 0.58f, cy - u * 0.06f);
                canvas.drawPath(path, paint);
                break;
            case CHAT:
                rect.set(cx - u * 0.86f, cy - u * 0.74f, cx + u * 0.86f, cy + u * 0.42f);
                canvas.drawRoundRect(rect, u * 0.44f, u * 0.44f, paint);
                path.reset();
                path.moveTo(cx - u * 0.22f, cy + u * 0.42f);
                path.lineTo(cx - u * 0.46f, cy + u * 0.82f);
                path.lineTo(cx + u * 0.06f, cy + u * 0.42f);
                canvas.drawPath(path, paint);
                break;
            case HEALTH:
                path.reset();
                path.moveTo(cx, cy + u * 0.78f);
                path.cubicTo(cx - u * 1.30f, cy - u * 0.10f,
                        cx - u * 0.60f, cy - u * 0.94f, cx, cy - u * 0.28f);
                path.cubicTo(cx + u * 0.60f, cy - u * 0.94f,
                        cx + u * 1.30f, cy - u * 0.10f, cx, cy + u * 0.78f);
                canvas.drawPath(path, paint);
                break;
            case ME:
                canvas.drawCircle(cx, cy - u * 0.36f, u * 0.36f, paint);
                path.reset();
                path.moveTo(cx - u * 0.74f, cy + u * 0.82f);
                path.cubicTo(cx - u * 0.70f, cy + u * 0.06f,
                        cx + u * 0.70f, cy + u * 0.06f, cx + u * 0.74f, cy + u * 0.82f);
                canvas.drawPath(path, paint);
                break;
            case BULB:
                canvas.drawCircle(cx, cy - u * 0.18f, u * 0.56f, paint);
                path.reset();
                path.moveTo(cx - u * 0.28f, cy + u * 0.42f);
                path.lineTo(cx + u * 0.28f, cy + u * 0.42f);
                path.moveTo(cx - u * 0.22f, cy + u * 0.68f);
                path.lineTo(cx + u * 0.22f, cy + u * 0.68f);
                canvas.drawPath(path, paint);
                break;
            case PLUG:
                rect.set(cx - u * 0.52f, cy - u * 0.12f, cx + u * 0.52f, cy + u * 0.62f);
                canvas.drawRoundRect(rect, u * 0.24f, u * 0.24f, paint);
                canvas.drawLine(cx - u * 0.24f, cy - u * 0.12f, cx - u * 0.24f, cy - u * 0.72f, paint);
                canvas.drawLine(cx + u * 0.24f, cy - u * 0.12f, cx + u * 0.24f, cy - u * 0.72f, paint);
                break;
            case AC:
                canvas.drawRoundRect(new RectF(cx - u * 0.86f, cy - u * 0.54f,
                        cx + u * 0.86f, cy + u * 0.18f), u * 0.24f, u * 0.24f, paint);
                path.reset();
                path.moveTo(cx - u * 0.34f, cy + u * 0.48f);
                path.lineTo(cx - u * 0.34f, cy + u * 0.86f);
                path.moveTo(cx, cy + u * 0.48f);
                path.lineTo(cx, cy + u * 0.90f);
                path.moveTo(cx + u * 0.34f, cy + u * 0.48f);
                path.lineTo(cx + u * 0.34f, cy + u * 0.86f);
                canvas.drawPath(path, paint);
                break;
            case SENSOR:
                canvas.drawCircle(cx, cy, u * 0.30f, paint);
                canvas.drawCircle(cx, cy, u * 0.72f, paint);
                break;
            case ROUTER:
                canvas.drawRoundRect(new RectF(cx - u * 0.78f, cy + u * 0.10f,
                        cx + u * 0.78f, cy + u * 0.70f), u * 0.20f, u * 0.20f, paint);
                canvas.drawLine(cx - u * 0.40f, cy + u * 0.10f, cx - u * 0.58f, cy - u * 0.62f, paint);
                canvas.drawLine(cx + u * 0.40f, cy + u * 0.10f, cx + u * 0.58f, cy - u * 0.62f, paint);
                break;
            case SEND:
                path.reset();
                path.moveTo(cx - u * 0.86f, cy - u * 0.62f);
                path.lineTo(cx + u * 0.88f, cy);
                path.lineTo(cx - u * 0.86f, cy + u * 0.62f);
                path.lineTo(cx - u * 0.52f, cy);
                path.close();
                paint.setStyle(Paint.Style.FILL_AND_STROKE);
                paint.setStrokeWidth(0f);
                canvas.drawPath(path, paint);
                paint.setStyle(Paint.Style.STROKE);
                paint.setStrokeWidth(getResources().getDisplayMetrics().density * 1.7f);
                break;
            default:
                rect.set(cx - u * 0.62f, cy - u * 0.62f, cx + u * 0.62f, cy + u * 0.62f);
                canvas.drawRoundRect(rect, u * 0.30f, u * 0.30f, paint);
                canvas.drawCircle(cx, cy, u * 0.14f, paint);
        }
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int size = Ui.dp(getContext(), 24);
        setMeasuredDimension(resolveSize(size, widthMeasureSpec), resolveSize(size, heightMeasureSpec));
    }
}
