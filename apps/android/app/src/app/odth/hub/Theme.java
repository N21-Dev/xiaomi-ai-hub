package app.odth.hub;

import android.content.Context;
import android.content.res.Configuration;
import android.util.DisplayMetrics;
import android.view.WindowManager;

/** 响应式断点与屏幕尺寸。手机 / 平板共用同一套页面，只换列数与导航形态。 */
final class Theme {

    /** 600dp 是 Material 的平板断点，米家平板端也在这个量级切换布局。 */
    static final int WIDE_DP = 600;
    static final int LARGE_DP = 840;

    private Theme() {
    }

    static int widthDp(Context context) {
        Configuration configuration = context.getResources().getConfiguration();
        int width = configuration.screenWidthDp;
        if (width > 0) return width;
        DisplayMetrics metrics = new DisplayMetrics();
        WindowManager manager = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        if (manager != null) manager.getDefaultDisplay().getMetrics(metrics);
        return (int) (metrics.widthPixels / metrics.density);
    }

    static boolean wide(Context context) {
        return widthDp(context) >= WIDE_DP;
    }

    static boolean large(Context context) {
        return widthDp(context) >= LARGE_DP;
    }

    /** 设备卡片列数：手机 1 列、平板 2 列、大平板 3 列。 */
    static int columns(Context context) {
        int width = widthDp(context);
        if (width >= LARGE_DP) return 3;
        if (width >= WIDE_DP) return 2;
        return 1;
    }

    /** 内容最大宽度：宽屏上不让文字横跨整屏，读起来更舒服。 */
    static int contentWidth(Context context) {
        int width = widthDp(context);
        if (width >= LARGE_DP) return 1080;
        if (width >= WIDE_DP) return 840;
        return width;
    }

    static int gutter(Context context) {
        return wide(context) ? 20 : 16;
    }
}
