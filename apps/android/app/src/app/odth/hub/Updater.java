package app.odth.hub;

import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Locale;

/**
 * 应用内更新：查版本 → 下载 → 校验 → 交给系统安装器。
 *
 * 安装包由中枢从 STATE_DIR/android/ 分发（`/api/app/android`），不经过任何应用商店。
 * 下载后先核 sha256 与签名，再发起安装：签名不一致系统也会拒装，但提前检查能给出人话提示。
 */
final class Updater {

    private static final int CONNECT_TIMEOUT = 6000;
    private static final int READ_TIMEOUT = 20000;
    private static final String MIME_APK = "application/vnd.android.package-archive";

    interface Progress {
        void onProgress(int percent);
    }

    static final class Latest {
        final int versionCode;
        final String versionName;
        final String sha256;
        final long size;
        final String notes;

        Latest(int versionCode, String versionName, String sha256, long size, String notes) {
            this.versionCode = versionCode;
            this.versionName = versionName == null ? "" : versionName;
            this.sha256 = sha256 == null ? "" : sha256;
            this.size = size;
            this.notes = notes == null ? "" : notes;
        }
    }

    private Updater() {
    }

    // ---------------------------------------------------------------- 本机版本

    static int versionCode(Context context) {
        try {
            return context.getPackageManager()
                    .getPackageInfo(context.getPackageName(), 0).versionCode;
        } catch (PackageManager.NameNotFoundException error) {
            return 0;
        }
    }

    static String versionName(Context context) {
        try {
            PackageInfo info = context.getPackageManager()
                    .getPackageInfo(context.getPackageName(), 0);
            return info.versionName == null ? "?" : info.versionName;
        } catch (PackageManager.NameNotFoundException error) {
            return "?";
        }
    }

    // ---------------------------------------------------------------- 查询

    /** 服务器上没发布过安装包时返回 null（不是错误，按「暂无更新」处理）。 */
    static Latest check(ServerConfig server) throws Exception {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(server.panelBase() + "/api/app/android")
                    .openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT);
            connection.setReadTimeout(READ_TIMEOUT);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("User-Agent", "HubAndroid");
            int status = connection.getResponseCode();
            if (status == 404) return null;
            if (status != 200) throw new Exception("服务器返回 HTTP " + status);
            String body = readAll(connection.getInputStream());
            JSONObject payload = new JSONObject(body);
            int code = payload.optInt("versionCode", 0);
            String name = payload.optString("versionName", "");
            if (code <= 0 || name.isEmpty()) throw new Exception("更新信息不完整");
            return new Latest(code, name, payload.optString("sha256", ""),
                    payload.optLong("size", 0), payload.optString("notes", ""));
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    // ---------------------------------------------------------------- 下载

    static File download(Context context, ServerConfig server, Latest latest,
                         Progress progress) throws Exception {
        File dir = new File(context.getCacheDir(), "update");
        if (!dir.exists() && !dir.mkdirs()) throw new IOException("无法创建缓存目录");
        File target = new File(dir, "Hub-" + latest.versionName + ".apk");
        File temp = new File(dir, "download.tmp");
        // 同目录下的旧包先清掉，避免缓存越攒越多
        File[] stale = dir.listFiles();
        if (stale != null) {
            for (File file : stale) {
                if (!file.equals(temp) && !file.equals(target)) file.delete();
            }
        }

        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection)
                    new URL(server.panelBase() + "/api/app/android/download").openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT);
            connection.setReadTimeout(READ_TIMEOUT);
            connection.setRequestProperty("Accept", "*/*");
            connection.setRequestProperty("User-Agent", "HubAndroid/" + latest.versionName);
            int status = connection.getResponseCode();
            if (status != 200) throw new Exception("下载失败，服务器返回 HTTP " + status);
            long total = connection.getContentLengthLong();
            if (total <= 0) total = latest.size;

            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long done = 0;
            int lastPercent = -1;
            byte[] buffer = new byte[16384];
            try (InputStream input = connection.getInputStream();
                 FileOutputStream out = new FileOutputStream(temp)) {
                int read;
                while ((read = input.read(buffer)) > 0) {
                    out.write(buffer, 0, read);
                    digest.update(buffer, 0, read);
                    done += read;
                    if (total > 0) {
                        int percent = (int) Math.min(100, done * 100 / total);
                        if (percent != lastPercent) {
                            lastPercent = percent;
                            progress.onProgress(percent);
                        }
                    }
                }
                out.flush();
            }

            if (latest.size > 0 && done != latest.size) {
                throw new Exception("下载不完整（" + done + "/" + latest.size + " 字节）");
            }
            if (!latest.sha256.isEmpty()) {
                String actual = hex(digest.digest());
                if (!actual.equalsIgnoreCase(latest.sha256)) {
                    throw new Exception("安装包校验失败，已放弃安装");
                }
            }
            if (target.exists()) target.delete();
            if (!temp.renameTo(target)) throw new IOException("无法保存安装包");
            verifyPackage(context, target);
            return target;
        } finally {
            if (connection != null) connection.disconnect();
            if (temp.exists()) temp.delete();
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder builder = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) builder.append(String.format(Locale.US, "%02x", value));
        return builder.toString();
    }

    /** 包名要一致、签名要和本机一致，否则系统也会拒装。 */
    @SuppressWarnings("deprecation")
    private static void verifyPackage(Context context, File apk) throws Exception {
        PackageManager pm = context.getPackageManager();
        PackageInfo archive = pm.getPackageArchiveInfo(apk.getAbsolutePath(),
                PackageManager.GET_SIGNATURES);
        if (archive == null || archive.signatures == null || archive.signatures.length == 0) {
            throw new Exception("安装包无法解析");
        }
        if (!context.getPackageName().equals(archive.packageName)) {
            throw new Exception("安装包不属于本应用");
        }
        PackageInfo installed = pm.getPackageInfo(context.getPackageName(),
                PackageManager.GET_SIGNATURES);
        if (installed.signatures == null || installed.signatures.length == 0) {
            throw new Exception("读不到本机签名");
        }
        if (!archive.signatures[0].equals(installed.signatures[0])) {
            throw new Exception("安装包签名与本机不一致，需要卸载后重装");
        }
    }

    // ---------------------------------------------------------------- 安装

    static boolean canInstall(Context context) {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                || context.getPackageManager().canRequestPackageInstalls();
    }

    /** Android 8 起要在系统设置里允许「安装未知应用」，跳过去让用户开。 */
    static void requestInstallPermission(Context context) {
        try {
            Intent intent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + context.getPackageName()));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
        } catch (Exception ignored) {
            // 少数 ROM 没有这个页面，退回应用详情页
            Intent fallback = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + context.getPackageName()));
            fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(fallback);
        }
    }

    static void install(Context context, File apk) {
        Uri uri = Uri.parse("content://" + context.getPackageName()
                + ApkProvider.AUTHORITY_SUFFIX + "/" + apk.getName());
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(uri, MIME_APK);
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
    }

    // ---------------------------------------------------------------- 工具

    private static String readAll(InputStream stream) throws IOException {
        StringBuilder builder = new StringBuilder();
        try (InputStream input = stream) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) > 0) {
                builder.append(new String(buffer, 0, read, StandardCharsets.UTF_8));
                if (builder.length() > 64 * 1024) break;
            }
        }
        return builder.toString();
    }
}
