import * as vscode from "vscode";

export class ArtifactPanel {
  public static currentPanel: ArtifactPanel | undefined;
  public static readonly viewType = "aiBackendBuilderArtifact";

  private readonly _panel: vscode.WebviewPanel;
  private readonly _extensionUri: vscode.Uri;
  private _disposables: vscode.Disposable[] = [];

  public static show(context: vscode.ExtensionContext, type: string, content: string) {
    let title = "Artifact";
    if (type === "class") title = "Class Diagram";
    if (type === "usecase") title = "Use Case Diagram";
    if (type === "swagger") title = "API Contract (Swagger)";

    if (ArtifactPanel.currentPanel) {
      ArtifactPanel.currentPanel._panel.reveal(vscode.ViewColumn.One);
      ArtifactPanel.currentPanel.update(title, type, content);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      ArtifactPanel.viewType,
      title,
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [context.extensionUri],
        retainContextWhenHidden: true,
      }
    );

    ArtifactPanel.currentPanel = new ArtifactPanel(panel, context.extensionUri);
    ArtifactPanel.currentPanel.update(title, type, content);
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
    ArtifactPanel.currentPanel = undefined;
    this._panel.dispose();

    while (this._disposables.length) {
      const x = this._disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }

  private update(title: string, type: string, content: string) {
    this._panel.title = title;
    this._panel.webview.html = this._getHtmlForWebview(this._panel.webview, title, type, content);
  }

  private _getHtmlForWebview(webview: vscode.Webview, title: string, type: string, content: string) {
    const nonce = getNonce();
    const escapedContent = JSON.stringify(content);
    
    let headInject = '';
    let bodyInject = '';
    
    if (type === 'swagger') {
      let code = content.trim();
      if (code.startsWith('```yaml')) code = code.replace(/^```yaml\s*\n/, '');
      if (code.startsWith('```')) code = code.replace(/^```\s*\n/, '');
      if (code.endsWith('```')) code = code.replace(/\n```$/, '');
      const escapedYaml = JSON.stringify(code);
      
      headInject = `
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui.css">
        <style>
          body { background: white; margin: 0; padding: 0; }
          .swagger-ui .topbar { display: none; }
          .toolbar { display: none; }
          .diagram-container { display: none; }
          #swagger-ui { height: 100vh; overflow: auto; padding: 20px; }
        </style>
      `;
      bodyInject = `
        <div id="swagger-ui"></div>
        <script nonce="${nonce}" src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui-bundle.js"></script>
        <script nonce="${nonce}" src="https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js"></script>
        <script nonce="${nonce}">
          window.onload = function() {
            try {
              const spec = jsyaml.load(${escapedYaml});
              SwaggerUIBundle({
                spec: spec,
                dom_id: '#swagger-ui',
                presets: [
                  SwaggerUIBundle.presets.apis,
                  SwaggerUIBundle.SwaggerUIStandalonePreset
                ],
                layout: "BaseLayout"
              });
            } catch (e) {
              document.getElementById('swagger-ui').innerHTML = '<div style="color:red;padding:20px;">Error parsing Swagger YAML: ' + e.message + '</div>';
            }
          };
        </script>
      `;
    } else {
      // Mermaid
      let code = content.trim();
      if (code.startsWith('\`\`\`mermaid')) code = code.replace(/^\`\`\`mermaid\s*\n/, '');
      if (code.startsWith('\`\`\`')) code = code.replace(/^\`\`\`\s*\n/, '');
      if (code.endsWith('\`\`\`')) code = code.replace(/\n\`\`\`$/, '');
      const escapedMermaid = JSON.stringify(code);
      
      bodyInject = `
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
          window.onerror = function(msg, url, lineNo, columnNo, error) {
            const container = document.getElementById('mermaid-render');
            if(container) container.innerHTML = '<div style="color:red;">Global JS Error: ' + msg + ' at line ' + lineNo + '</div>';
            return false;
          };

          const vscode = acquireVsCodeApi();
          try {
            mermaid.initialize({ 
            startOnLoad: false,
            theme: 'default',
            securityLevel: 'loose',
            fontFamily: 'sans-serif'
          });

          const mermaidCode = ${escapedMermaid};
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
            
            const bbox = svg.getBoundingClientRect();
            canvas.width = bbox.width || 800;
            canvas.height = bbox.height || 600;
            
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
            
            img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svgData)));
          });

          renderDiagram();
          } catch (e) {
            const container = document.getElementById('mermaid-render');
            if(container) container.innerHTML = '<div style="color:red;">Sync Initialization Error: ' + e.message + '</div>';
          }
        </script>
      `;
    }

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
  ${headInject}
</head>
<body>
  ${bodyInject}
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
