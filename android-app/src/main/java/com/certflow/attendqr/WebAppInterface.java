package com.certflow.attendqr;

import android.webkit.JavascriptInterface;

public class WebAppInterface {
    private MainActivity activity;

    public WebAppInterface(MainActivity activity) {
        this.activity = activity;
    }

    @JavascriptInterface
    public void saveFile(String base64Data, String filename, String mimeType) {
        if (activity != null) {
            activity.saveFileToDownloads(base64Data, filename, mimeType);
        }
    }
}
