package com.certflow.attendqr;

import android.content.Context;
import android.graphics.Bitmap;
import android.view.View;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.Toast;

public class CustomWebViewClient extends WebViewClient {
    private ProgressBar progressBar;
    private Context context;

    public CustomWebViewClient(Context context, ProgressBar progressBar) {
        this.context = context;
        this.progressBar = progressBar;
    }

    @Override
    public void onPageStarted(WebView view, String url, Bitmap favicon) {
        if (progressBar != null) {
            progressBar.setVisibility(View.VISIBLE);
        }
    }

    @Override
    public void onPageFinished(WebView view, String url) {
        if (progressBar != null) {
            progressBar.setVisibility(View.GONE);
        }
    }

    @Override
    public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
        if (request.isForMainFrame()) {
            if (progressBar != null) {
                progressBar.setVisibility(View.GONE);
            }
            if (context != null) {
                Toast.makeText(context, "Cannot connect to server. Check IP in Settings ⚙️", Toast.LENGTH_LONG).show();
            }
        }
    }
}
