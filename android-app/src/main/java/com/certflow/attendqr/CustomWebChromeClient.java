package com.certflow.attendqr;

import android.net.Uri;
import android.view.View;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.widget.ProgressBar;

public class CustomWebChromeClient extends WebChromeClient {
    private MainActivity activity;
    private ProgressBar progressBar;

    public CustomWebChromeClient(MainActivity activity, ProgressBar progressBar) {
        this.activity = activity;
        this.progressBar = progressBar;
    }

    @Override
    public void onPermissionRequest(PermissionRequest request) {
        if (request != null) {
            request.grant(request.getResources());
        }
    }

    @Override
    public void onProgressChanged(WebView view, int newProgress) {
        if (progressBar != null && newProgress == 100) {
            progressBar.setVisibility(View.GONE);
        }
    }

    @Override
    public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
        if (activity != null) {
            activity.openFileChooser(filePathCallback);
            return true;
        }
        return false;
    }
}
