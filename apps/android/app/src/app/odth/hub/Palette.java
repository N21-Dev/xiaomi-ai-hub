package app.odth.hub;

/**
 * 两套配色令牌。浅色是米家那套浅灰底 + 白卡片；深色是近黑底 + 深灰卡片，主色提亮一档。
 *
 * 所有页面都只引用 {@link Ui} 里的静态颜色字段，切换主题时把整组值重新赋一遍，
 * 再让界面重新构建即可，不需要每个控件各写一套判断。
 */
final class Palette {

    static final class Tokens {
        final int bg;
        final int surface;
        final int surface2;
        final int divider;
        final int outline;
        final int text;
        final int text2;
        final int text3;
        final int accent;
        final int accentSoft;
        final int accentText;
        final int warn;
        final int warnSoft;
        final int danger;
        final int blue;
        final int switchOff;
        final int codeBg;

        Tokens(int bg, int surface, int surface2, int divider, int outline,
               int text, int text2, int text3,
               int accent, int accentSoft, int accentText,
               int warn, int warnSoft, int danger, int blue, int switchOff, int codeBg) {
            this.bg = bg;
            this.surface = surface;
            this.surface2 = surface2;
            this.divider = divider;
            this.outline = outline;
            this.text = text;
            this.text2 = text2;
            this.text3 = text3;
            this.accent = accent;
            this.accentSoft = accentSoft;
            this.accentText = accentText;
            this.warn = warn;
            this.warnSoft = warnSoft;
            this.danger = danger;
            this.blue = blue;
            this.switchOff = switchOff;
            this.codeBg = codeBg;
        }
    }

    // 对比度按 WCAG AA 校过：正文 4.5:1、图标与大字 3:1。
    // 浅色的主色/弱化文字/警告色都比第一版更深——原来的亮青绿配白字只有 2.69:1，
    // 「已开启」这类小字在阳光下基本看不清。
    static final Tokens LIGHT = new Tokens(
            0xFFF5F6F8,  // bg
            0xFFFFFFFF,  // surface
            0xFFF2F4F7,  // surface2
            0xFFEDEFF3,  // divider
            0xFFE6E9EF,  // outline
            0xFF1B1E23,  // text      16.7:1
            0xFF5A6472,  // text2      6.0:1
            0xFF697180,  // text3      4.9:1
            0xFF00795C,  // accent     白字 5.4:1
            0xFFE6F7F2,  // accentSoft
            0xFFFFFFFF,  // accentText
            0xFF9A5B00,  // warn       5.4:1
            0xFFFDF3E3,  // warnSoft
            0xFFC7303A,  // danger     5.4:1
            0xFF1B6BD6,  // blue
            0xFFE3E6EB,  // switchOff
            0xFFEEF1E9   // codeBg
    );

    static final Tokens DARK = new Tokens(
            0xFF0E1013,  // bg
            0xFF171A1E,  // surface
            0xFF1E2227,  // surface2
            0xFF23272D,  // divider
            0xFF2A2F36,  // outline
            0xFFF1F3F5,  // text
            0xFFA6ADB7,  // text2
            0xFF7F8792,  // text3      4.8:1
            0xFF2BD6A8,  // accent（深底上需要更亮一档）
            0xFF14322C,  // accentSoft
            0xFF06231B,  // accentText（亮青绿上用深色字才够对比）
            0xFFF2B457,  // warn
            0xFF2E2517,  // warnSoft
            0xFFFF6B6E,  // danger
            0xFF6AA6FF,  // blue
            0xFF343A42,  // switchOff
            0xFF1C2025   // codeBg
    );

    private Palette() {
    }

    private static boolean darkApplied = false;

    /** 供少数需要按主题选系统样式的地方（例如对话框）使用。 */
    static boolean isDark() {
        return darkApplied;
    }

    /** 把一整套颜色写进 Ui 的静态字段。调用方负责随后重建界面。 */
    static void apply(boolean dark) {
        darkApplied = dark;
        Tokens tokens = dark ? DARK : LIGHT;
        Ui.BG = tokens.bg;
        Ui.SURFACE = tokens.surface;
        Ui.SURFACE_2 = tokens.surface2;
        Ui.DIVIDER = tokens.divider;
        Ui.OUTLINE = tokens.outline;
        Ui.TEXT = tokens.text;
        Ui.TEXT_2 = tokens.text2;
        Ui.TEXT_3 = tokens.text3;
        Ui.ACCENT = tokens.accent;
        Ui.ACCENT_SOFT = tokens.accentSoft;
        Ui.ACCENT_TEXT = tokens.accentText;
        Ui.WARN = tokens.warn;
        Ui.WARN_SOFT = tokens.warnSoft;
        Ui.DANGER = tokens.danger;
        Ui.BLUE = tokens.blue;
        Ui.SWITCH_OFF = tokens.switchOff;
        Ui.CODE_BG = tokens.codeBg;
    }
}
