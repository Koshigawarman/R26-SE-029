import * as vscode from "vscode";
import * as fs from "fs";

export function getPanelHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  nonce: string
): string {
  const htmlPath = vscode.Uri.joinPath(extensionUri, "media", "webview.html");

  try {
    // Read the HTML file synchronously from disk
    let html = fs.readFileSync(htmlPath.fsPath, "utf8");

    // Inject the CSP nonce and webview source
    html = html.replace(/{{nonce}}/g, nonce);
    html = html.replace(/{{cspSource}}/g, webview.cspSource);

    return html;
  } catch (err: any) {
    console.error("Failed to load webview.html", err);
    return `<!DOCTYPE html>
          <html lang="en">
          <head>
            <meta charset="UTF-8"/>
            <title>Error</title>
          </head>
          <body>
            <h2>Error loading UI</h2>
            <p>${err.message}</p>
          </body>
          </html>`;
  }
}
