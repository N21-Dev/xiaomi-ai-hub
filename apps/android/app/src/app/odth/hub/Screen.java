package app.odth.hub;

import android.view.View;

/** 一个页签的内容。刷新时由 Activity 统一触发。 */
interface Screen {
    View view();

    void refresh();
}
