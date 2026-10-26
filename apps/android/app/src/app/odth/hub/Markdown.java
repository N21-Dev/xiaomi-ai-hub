package app.odth.hub;

import android.graphics.Typeface;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.BackgroundColorSpan;
import android.text.style.ForegroundColorSpan;
import android.text.style.RelativeSizeSpan;
import android.text.style.StrikethroughSpan;
import android.text.style.StyleSpan;
import android.text.style.TypefaceSpan;
import android.text.style.UnderlineSpan;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 把助手回复的 Markdown 转成可显示的富文本。
 *
 * 只做聊天里真正会出现的子集：标题、列表、引用、围栏代码、分隔线、表格、粗斜体、
 * 行内代码、删除线、链接。不引第三方库，也不解析 HTML —— 模型输出的尖括号一律当普通文字。
 *
 * 链接只做下划线 + 主题色，不加点击：气泡在可滚动容器里，LinkMovementMethod 会跟
 * 滚动抢手势，宁可不点也不要滚不动。
 */
final class Markdown {

    private static final Pattern INLINE = Pattern.compile(
            "(`[^`\\n]+`)"
                    + "|(\\*\\*[^*\\n]+\\*\\*)"
                    + "|(__[^_\\n]+__)"
                    + "|(~~[^~\\n]+~~)"
                    + "|(\\*[^*\\n]+\\*)"
                    + "|(_[^_\\n]+_)"
                    + "|(\\[[^\\]\\n]+\\]\\([^)\\s]+\\))"
                    + "|(https?://[^\\s<>()]+)");
    private static final Pattern FENCE = Pattern.compile("^\\s*(`{3,}|~{3,})\\s*([\\w+#.-]*)\\s*$");
    private static final Pattern HEADING = Pattern.compile("^\\s*(#{1,6})\\s+(.*)$");
    private static final Pattern BULLET = Pattern.compile("^(\\s*)([-*+]|\\d+[.)])\\s+(.*)$");
    private static final Pattern RULE = Pattern.compile("^\\s*([-*_])\\1{2,}\\s*$");
    private static final Pattern TABLE_RULE = Pattern.compile("^\\s*\\|?[\\s:|-]+\\|.*$");

    private Markdown() {
    }

    static CharSequence render(String source, int text, int dim, int accent) {
        SpannableStringBuilder out = new SpannableStringBuilder();
        if (source == null || source.isEmpty()) return out;
        String[] lines = source.replace("\r\n", "\n").replace('\r', '\n').split("\n", -1);
        int index = 0;
        boolean gap = false;   // 上一个块是代码/表格/引用时，后面留一个空行
        while (index < lines.length) {
            String line = lines[index];
            if (line.trim().isEmpty()) {
                index++;
                continue;
            }
            boolean spaced = isSpacedBlock(lines, index);
            if (out.length() > 0) out.append(gap || spaced ? "\n\n" : "\n");
            gap = spaced;

            Matcher fence = FENCE.matcher(line);
            if (fence.matches()) {
                String mark = fence.group(1);
                int close = index + 1;
                StringBuilder body = new StringBuilder();
                while (close < lines.length && !isFenceEnd(lines[close], mark)) {
                    if (body.length() > 0) body.append('\n');
                    body.append(lines[close]);
                    close++;
                }
                index = close + 1;
                codeBlock(out, body.toString(), dim);
                continue;
            }

            Matcher heading = HEADING.matcher(line);
            if (heading.matches()) {
                int level = Math.min(3, heading.group(1).length());
                String content = heading.group(2).replaceAll("\\s+#+\\s*$", "");
                headingLine(out, content, level, text, dim, accent);
                index++;
                continue;
            }

            if (RULE.matcher(line).matches()) {
                rule(out, dim);
                index++;
                continue;
            }

            if (line.trim().startsWith(">")) {
                while (index < lines.length && lines[index].trim().startsWith(">")) {
                    if (index > 0) out.append("");
                    String content = lines[index].trim().replaceFirst("^>\\s?", "");
                    int start = out.length();
                    out.append("▍ ");
                    inline(out, content, dim, accent);
                    out.setSpan(new ForegroundColorSpan(dim), start, out.length(),
                            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
                    index++;
                    if (index < lines.length && lines[index].trim().startsWith(">")) out.append('\n');
                }
                continue;
            }

            if (BULLET.matcher(line).matches()) {
                int[] counter = {1};
                boolean firstItem = true;
                while (index < lines.length) {
                    Matcher item = BULLET.matcher(lines[index]);
                    if (!item.matches()) break;
                    boolean ordered = !"-".equals(item.group(2)) && !"*".equals(item.group(2))
                            && !"+".equals(item.group(2));
                    boolean nested = item.group(1).length() >= 2;
                    if (!firstItem) out.append('\n');
                    firstItem = false;
                    if (nested) out.append("　　");
                    String marker = ordered ? (counter[0]++) + ". " : "• ";
                    if (!ordered) counter[0] = 1;
                    int start = out.length();
                    out.append(marker);
                    inline(out, item.group(3), dim, accent);
                    out.setSpan(new ForegroundColorSpan(dim), start, start + marker.length(),
                            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
                    index++;
                }
                continue;
            }

            if (line.contains("|") && index + 1 < lines.length && TABLE_RULE.matcher(lines[index + 1]).matches()) {
                index = table(out, lines, index, text, dim, accent);
                continue;
            }

            boolean firstLine = true;
            while (index < lines.length && !isBlockStart(lines, index)) {
                if (!firstLine) out.append('\n');
                firstLine = false;
                inline(out, lines[index].trim(), dim, accent);
                index++;
                // 段落内的软换行合并成一行，避免手机上被切成碎句
                if (index < lines.length && !lines[index].trim().isEmpty() && !isBlockStart(lines, index)) {
                    out.append(' ');
                }
            }
        }
        return out;
    }

    // ---------------------------------------------------------------- 行内

    private static void inline(SpannableStringBuilder out, String source, int dim, int accent) {
        if (source == null || source.isEmpty()) return;
        Matcher matcher = INLINE.matcher(source);
        int cursor = 0;
        while (matcher.find()) {
            if (matcher.start() > cursor) out.append(source, cursor, matcher.start());
            String token = matcher.group();
            cursor = matcher.end();
            if (matcher.group(1) != null) {
                int start = out.length();
                out.append(token, 1, token.length() - 1);
                out.setSpan(new TypefaceSpan("monospace"), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
                out.setSpan(new BackgroundColorSpan(Ui.CODE_BG), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            } else if (matcher.group(2) != null || matcher.group(3) != null) {
                int start = out.length();
                inline(out, token.substring(2, token.length() - 2), dim, accent);
                out.setSpan(new StyleSpan(Typeface.BOLD), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            } else if (matcher.group(4) != null) {
                int start = out.length();
                inline(out, token.substring(2, token.length() - 2), dim, accent);
                out.setSpan(new StrikethroughSpan(), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            } else if (matcher.group(5) != null || matcher.group(6) != null) {
                int start = out.length();
                inline(out, token.substring(1, token.length() - 1), dim, accent);
                out.setSpan(new StyleSpan(Typeface.ITALIC), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            } else if (matcher.group(7) != null) {
                int cut = token.indexOf("](");
                String label = token.substring(1, cut);
                link(out, label.isEmpty() ? token : label, accent);
            } else {
                link(out, token, accent);
            }
        }
        if (cursor < source.length()) out.append(source, cursor, source.length());
    }

    private static void link(SpannableStringBuilder out, String label, int accent) {
        int start = out.length();
        out.append(label);
        out.setSpan(new UnderlineSpan(), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new ForegroundColorSpan(accent), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    // ---------------------------------------------------------------- 块级

    private static void headingLine(SpannableStringBuilder out, String content, int level,
                                    int text, int dim, int accent) {
        int start = out.length();
        inline(out, content, dim, accent);
        out.setSpan(new StyleSpan(Typeface.BOLD), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new RelativeSizeSpan(level == 1 ? 1.26f : level == 2 ? 1.16f : 1.06f),
                start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new ForegroundColorSpan(text), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    private static void rule(SpannableStringBuilder out, int dim) {
        int start = out.length();
        out.append("────────────");
        out.setSpan(new ForegroundColorSpan(dim), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new RelativeSizeSpan(0.7f), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    private static void codeBlock(SpannableStringBuilder out, String body, int dim) {
        int start = out.length();
        out.append(body);
        out.setSpan(new TypefaceSpan("monospace"), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new BackgroundColorSpan(Ui.CODE_BG), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new RelativeSizeSpan(0.94f), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.setSpan(new ForegroundColorSpan(dim), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    /** 表格在手机上排不下真正的列，压成「甲 ｜ 乙」，表头加粗并补一条分隔。 */
    private static int table(SpannableStringBuilder out, String[] lines, int index,
                             int text, int dim, int accent) {
        int start = out.length();
        String[] header = cells(lines[index]);
        for (int i = 0; i < header.length; i++) {
            if (i > 0) out.append("  ｜  ");
            inline(out, header[i], dim, accent);
        }
        out.setSpan(new StyleSpan(Typeface.BOLD), start, out.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        out.append('\n');
        rule(out, dim);
        index += 2;
        while (index < lines.length && lines[index].contains("|") && !lines[index].trim().isEmpty()) {
            out.append('\n');
            String[] row = cells(lines[index]);
            for (int i = 0; i < row.length; i++) {
                if (i > 0) out.append("  ｜  ");
                inline(out, row[i], dim, accent);
            }
            index++;
        }
        return index;
    }

    private static String[] cells(String line) {
        String trimmed = line.trim();
        if (trimmed.startsWith("|")) trimmed = trimmed.substring(1);
        if (trimmed.endsWith("|")) trimmed = trimmed.substring(0, trimmed.length() - 1);
        String[] parts = trimmed.split("\\|", -1);
        for (int i = 0; i < parts.length; i++) parts[i] = parts[i].trim();
        return parts;
    }

    private static boolean isFenceEnd(String line, String mark) {
        String expected = mark.substring(0, 1);
        String trimmed = line.trim();
        if (trimmed.length() < mark.length()) return false;
        for (int i = 0; i < trimmed.length(); i++) {
            if (trimmed.charAt(i) != expected.charAt(0)) return false;
        }
        return true;
    }

    /** 这些块前后要空一行，否则手机上会和上下文粘成一片。 */
    private static boolean isSpacedBlock(String[] lines, int index) {
        String line = lines[index];
        if (FENCE.matcher(line).matches()) return true;
        if (HEADING.matcher(line).matches()) return true;
        if (RULE.matcher(line).matches()) return true;
        if (line.trim().startsWith(">")) return true;
        if (BULLET.matcher(line).matches()) return true;
        return line.contains("|") && index + 1 < lines.length
                && TABLE_RULE.matcher(lines[index + 1]).matches();
    }

    private static boolean isBlockStart(String[] lines, int index) {
        String line = lines[index];
        if (line.trim().isEmpty()) return true;
        if (FENCE.matcher(line).matches()) return true;
        if (HEADING.matcher(line).matches()) return true;
        if (RULE.matcher(line).matches()) return true;
        if (line.trim().startsWith(">")) return true;
        if (BULLET.matcher(line).matches()) return true;
        return line.contains("|") && index + 1 < lines.length
                && TABLE_RULE.matcher(lines[index + 1]).matches();
    }
}
