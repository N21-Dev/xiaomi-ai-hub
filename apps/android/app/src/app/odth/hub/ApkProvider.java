package app.odth.hub;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 把缓存目录里的安装包交给系统安装器。
 *
 * Android 7 起不能再用 file:// 打开别的应用进程能读的文件（FileUriExposedException），
 * 标准做法是 FileProvider —— 但它在 AndroidX 里，本项目不引 AndroidX，所以自己写一个最小实现：
 * 只读、只认 cacheDir/update 下的 .apk，其余一律拒绝。
 */
public final class ApkProvider extends ContentProvider {

    static final String AUTHORITY_SUFFIX = ".apk";
    private static final String MIME_APK = "application/vnd.android.package-archive";

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new FileNotFoundException("只读");
        String name = uri.getLastPathSegment();
        if (name == null || name.isEmpty() || name.contains("/") || name.contains("..")
                || !name.endsWith(".apk")) {
            throw new FileNotFoundException(String.valueOf(name));
        }
        if (getContext() == null) throw new FileNotFoundException("no context");
        File file = new File(new File(getContext().getCacheDir(), "update"), name);
        if (!file.exists()) throw new FileNotFoundException(name);
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public String getType(Uri uri) {
        return MIME_APK;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
