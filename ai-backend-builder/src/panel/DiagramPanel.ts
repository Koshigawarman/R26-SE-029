import * as vscode from "vscode";

export class DiagramPanel {
  public static currentPanel: DiagramPanel | undefined;
  public static readonly viewType = "aiBackendBuilderDiagram";

  private readonly _panel: vscode.WebviewPanel;
  private readonly _extensionUri: vscode.Uri;
  private _disposables: vscode.Disposable[] = [];

  public static show(context: vscode.ExtensionContext, type: string, mermaidCode: string) {
    const title = type === "class" ? "Class Diagram" : "Use Case Diagram";

    if (DiagramPanel.currentPanel) {
      DiagramPanel.currentPanel._panel.reveal(vscode.ViewColumn.One);
      DiagramPanel.currentPanel.update(title, mermaidCode);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      DiagramPanel.viewType,
      title,
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [context.extensionUri],
        retainContextWhenHidden: true,
      }
    );

    DiagramPanel.currentPanel = new DiagramPanel(panel, context.extensionUri);
    DiagramPanel.currentPanel.update(title, mermaidCode);
  }

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri) {
    this._panel = panel;
    this._extensionUri = extensionUri;

    this._panel.onDidDispose(() => this.dispose(), null, this._disposables);
    
    this._panel.webview.onDidReceiveMessage(
      async (message) => {
        switch (message.command) {
          case 'exportDiagram':
            await this._exportDiagram(message.dataUrl, message.title);
            break;
        }
      },
      null,
      this._disposables
    );
  }

  private async _exportDiagram(dataUrl: string, title: string) {
    const defaultTitle = title.replace(/\s+/g, '_').toLowerCase() + '.png';
    
    const workspaceFolders = vscode.workspace.workspaceFolders;
    let defaultUri = vscode.Uri.file(defaultTitle);
    if (workspaceFolders && workspaceFolders.length > 0) {
      defaultUri = vscode.Uri.joinPath(workspaceFolders[0].uri, defaultTitle);
    }

    const uri = await vscode.window.showSaveDialog({
      defaultUri: defaultUri,
      filters: {
        'Images': ['png']
      },
      title: 'Export Diagram'
    });

    if (uri) {
      try {
        const base64Data = dataUrl.replace(/^data:image\/png;base64,/, "");
        const buffer = Buffer.from(base64Data, 'base64');
        await vscode.workspace.fs.writeFile(uri, new Uint8Array(buffer));
        vscode.window.showInformationMessage('Diagram exported successfully!');
      } catch (err: any) {
        vscode.window.showErrorMessage(`Failed to export diagram: ${err.message}`);
      }
    }
  }

  public dispose() {
    DiagramPanel.currentPanel = undefined;
    this._panel.dispose();

    while (this._disposables.length) {
      const x = this._disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }

  private update(title: string, mermaidCode: string) {
    this._panel.title = title;
    this._panel.webview.html = this._getHtmlForWebview(this._panel.webview, title, mermaidCode);
  }

  private _getHtmlForWebview(webview: vscode.Webview, title: string, mermaidCode: string) {
    const nonce = getNonce();
    
    // Clean up mermaid code
    let code = mermaidCode.trim();
    if (code.startsWith('```mermaid')) {
      code = code.replace(/^```mermaid\s*\n/, '');
    }
    if (code.startsWith('```')) {
      code = code.replace(/^```\s*\n/, '');
    }
    if (code.endsWith('```')) {
      code = code.replace(/\n```$/, '');
    }
    
    // Escape for JSON string injection
    const escapedCode = JSON.stringify(code);

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline' https://cdn.jsdelivr.net; font-src ${webview.cspSource} https://cdn.jsdelivr.net; script-src 'nonce-${nonce}' https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data: https:; connect-src *;">
  <title>${title}</title>
  <style>
    body {
      padding: 0;
      margin: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      background-color: var(--vscode-editor-background);
      color: var(--vscode-editor-foreground);
      font-family: var(--vscode-font-family);
    }
    .toolbar {
      padding: 12px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background-color: var(--vscode-sideBar-background);
      border-bottom: 1px solid var(--vscode-widget-border);
      flex-shrink: 0;
    }
    .toolbar h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
    }
    .btn {
      background-color: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      padding: 6px 12px;
      cursor: pointer;
      border-radius: 4px;
      font-size: 13px;
      font-weight: 500;
    }
    .btn:hover {
      background-color: var(--vscode-button-hoverBackground);
    }
    .diagram-container {
      flex: 1;
      overflow: auto;
      padding: 20px;
      display: flex;
      justify-content: center;
      align-items: center;
      background-color: var(--vscode-editor-background);
    }
    #mermaid-render {
      background-color: white;
      padding: 20px;
      border-radius: 8px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
      width: 90%;
      height: 80vh;
      display: flex;
      justify-content: center;
      align-items: center;
      overflow: hidden;
    }
    .loading {
      color: var(--vscode-descriptionForeground);
      font-style: italic;
    }
  </style>
</head>
<body>
  <div class="toolbar">
    <h2>${title}</h2>
    <button class="btn" id="exportBtn">Export as PNG</button>
  </div>
  <div class="diagram-container">
    <div id="mermaid-render">
      <div class="loading">Rendering diagram...</div>
    </div>
  </div>

  <script nonce="${nonce}" src="https://cdn.jsdelivr.net/npm/mermaid@10.8.0/dist/mermaid.min.js"></script>
  <script nonce="${nonce}" src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js"></script>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    mermaid.initialize({ 
      startOnLoad: false,
      theme: 'default',
      securityLevel: 'loose',
      fontFamily: 'sans-serif'
    });

    const mermaidCode = ${escapedCode};
    const container = document.getElementById('mermaid-render');

    async function renderDiagram() {
      try {
        const { svg } = await mermaid.render('mermaid-svg', mermaidCode);
        container.innerHTML = svg;
        
        const svgElement = container.querySelector('svg');
        if (svgElement) {
          svgElement.style.maxWidth = 'none';
          svgElement.style.height = '100%';
          svgElement.style.width = '100%';
          
          svgPanZoom(svgElement, {
            zoomEnabled: true,
            controlIconsEnabled: true,
            fit: true,
            center: true,
            minZoom: 0.1,
            maxZoom: 10
          });
        }
      } catch (err) {
        container.innerHTML = '<div style="color:var(--vscode-editorError-foreground);">Error rendering diagram: ' + err.message + '<br><br><pre style="text-align:left;font-size:12px;">' + mermaidCode + '</pre></div>';
      }
    }

    document.getElementById('exportBtn').addEventListener('click', () => {
      const svg = container.querySelector('svg');
      if (!svg) return;
      
      const svgData = new XMLSerializer().serializeToString(svg);
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      
      // Get exact dimensions from SVG
      const bbox = svg.getBoundingClientRect();
      canvas.width = bbox.width || 800;
      canvas.height = bbox.height || 600;
      
      // Fix background to white before drawing
      ctx.fillStyle = "white";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      
      const img = new Image();
      img.onload = function() {
        ctx.drawImage(img, 0, 0);
        vscode.postMessage({
          command: 'exportDiagram',
          dataUrl: canvas.toDataURL("image/png"),
          title: "${title}"
        });
      };
      
      // Base64 encode SVG
      img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svgData)));
    });

    // Render on load
    renderDiagram();
  </script>
</body>
</html>`;
  }
}

function getNonce(): string {
  let text = '';
  const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
