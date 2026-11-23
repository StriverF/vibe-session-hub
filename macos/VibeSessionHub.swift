import AppKit
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backendProcess: Process?
    var syncTimer: Timer?
    let hubURL = URL(string: "http://127.0.0.1:17380")!

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMainMenu()
        setupWindow()
        applyBundleDockIcon()
        ensureBackendAndLoad()
        startICloudBridgeTimer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        syncICloudBridgeAsync()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let first = urls.first else { return }
        if first.scheme?.lowercased() == "vibehub" {
            let query = first.query ?? ""
            if let target = URL(string: "http://127.0.0.1:17380/jump?\(query)") {
                window.makeKeyAndOrderFront(nil)
                webView.load(URLRequest(url: target))
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let proc = backendProcess, proc.isRunning {
            proc.terminate()
        }
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "重新加载", action: #selector(reloadPage), keyEquivalent: "r")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "隐藏 Vibe Session Hub", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "关闭窗口", action: #selector(closeCurrentWindow), keyEquivalent: "w")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出 Vibe Session Hub", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func reloadPage() {
        webView.load(URLRequest(url: hubURL))
    }

    @objc private func closeCurrentWindow() {
        window.orderOut(nil)
    }

    private func setupWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1120, height: 740)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.center()
        window.title = "Vibe Session Hub"
        window.minSize = NSSize(width: 760, height: 500)
        window.isReleasedWhenClosed = false

        let cfg = WKWebViewConfiguration()
        webView = WKWebView(frame: rect, configuration: cfg)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
    }

    private func applyBundleDockIcon() {
        let icnsURL = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/AppIcon.icns")
        if let data = try? Data(contentsOf: icnsURL),
           let img = NSImage(data: data) {
            NSApp.applicationIconImage = img
        }
    }

    private func ensureBackendAndLoad() {
        checkHealth { [weak self] healthy in
            guard let self = self else { return }
            if healthy {
                DispatchQueue.main.async {
                    self.webView.load(URLRequest(url: self.hubURL))
                }
                return
            }
            self.spawnFallbackBackend()
            self.pollUntilReady(attemptsLeft: 25)
        }
    }

    private func checkHealth(completion: @escaping (Bool) -> Void) {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:17380/api/health")!)
        req.timeoutInterval = 0.6
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            completion(code == 200)
        }.resume()
    }

    private func spawnFallbackBackend() {
        guard let resPath = Bundle.main.resourcePath else { return }
        let rootPointer = URL(fileURLWithPath: resPath).appendingPathComponent("project_root.txt")
        guard let projectRoot = try? String(contentsOf: rootPointer, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              !projectRoot.isEmpty else { return }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = ["-m", "hub.server"]
        proc.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        try? proc.run()
        backendProcess = proc
    }

    private func startICloudBridgeTimer() {
        syncICloudBridgeAsync()
        syncTimer = Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            self?.syncICloudBridgeAsync()
        }
    }

    private func syncICloudBridgeAsync() {
        DispatchQueue.global(qos: .utility).async {
            let fm = FileManager.default
            let home = fm.homeDirectoryForCurrentUser
            let localSync = home.appendingPathComponent(".vibe-session-hub/sync")
            let icloudBase = home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub")
            guard fm.fileExists(atPath: home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs").path) else {
                return
            }

            var pulledRemoteChange = false
            for sub in ["nodes", "manual_links"] {
                let lDir = localSync.appendingPathComponent(sub)
                let cDir = icloudBase.appendingPathComponent(sub)
                try? fm.createDirectory(at: lDir, withIntermediateDirectories: true)
                try? fm.createDirectory(at: cDir, withIntermediateDirectories: true)

                if let lFiles = try? fm.contentsOfDirectory(at: lDir, includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey]) {
                    for lf in lFiles where lf.pathExtension == "jsonl" && !lf.lastPathComponent.hasPrefix(".") {
                        let cf = cDir.appendingPathComponent(lf.lastPathComponent)
                        let lAttr = try? fm.attributesOfItem(atPath: lf.path)
                        let cAttr = try? fm.attributesOfItem(atPath: cf.path)
                        let lDate = (lAttr?[.modificationDate] as? Date) ?? .distantPast
                        let cDate = (cAttr?[.modificationDate] as? Date) ?? .distantPast
                        let lSize = (lAttr?[.size] as? Int) ?? 0
                        let cSize = (cAttr?[.size] as? Int) ?? -1
                        if !fm.fileExists(atPath: cf.path) || (lDate > cDate && lSize != cSize) {
                            if let data = try? Data(contentsOf: lf) {
                                try? data.write(to: cf)
                            }
                        }
                    }
                }

                if let cFiles = try? fm.contentsOfDirectory(at: cDir, includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey]) {
                    for cf in cFiles where cf.pathExtension == "jsonl" && !cf.lastPathComponent.hasPrefix(".") {
                        let lf = lDir.appendingPathComponent(cf.lastPathComponent)
                        let cAttr = try? fm.attributesOfItem(atPath: cf.path)
                        let lAttr = try? fm.attributesOfItem(atPath: lf.path)
                        let cDate = (cAttr?[.modificationDate] as? Date) ?? .distantPast
                        let lDate = (lAttr?[.modificationDate] as? Date) ?? .distantPast
                        let cSize = (cAttr?[.size] as? Int) ?? 0
                        let lSize = (lAttr?[.size] as? Int) ?? -1
                        if !fm.fileExists(atPath: lf.path) || (cDate > lDate && cSize != lSize) {
                            if let data = try? Data(contentsOf: cf) {
                                try? data.write(to: lf, options: .atomic)
                                pulledRemoteChange = true
                            }
                        }
                    }
                }
            }

            if pulledRemoteChange {
                var req = URLRequest(url: URL(string: "http://127.0.0.1:17380/api/sync")!)
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.setValue("1", forHTTPHeaderField: "X-Vibe-Hub")
                req.httpBody = Data("{}".utf8)
                URLSession.shared.dataTask(with: req).resume()
            }
        }
    }

    private func pollUntilReady(attemptsLeft: Int) {
        if attemptsLeft <= 0 {
            DispatchQueue.main.async {
                self.webView.load(URLRequest(url: self.hubURL))
            }
            return
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self = self else { return }
            self.checkHealth { ok in
                if ok {
                    DispatchQueue.main.async {
                        self.webView.load(URLRequest(url: self.hubURL))
                    }
                } else {
                    self.pollUntilReady(attemptsLeft: attemptsLeft - 1)
                }
            }
        }
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        if let url = navigationAction.request.url,
           let scheme = url.scheme?.lowercased(),
           scheme != "http" && scheme != "https" && scheme != "about" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        if let url = navigationAction.request.url,
           let host = url.host?.lowercased(),
           host != "127.0.0.1" && host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        input.stringValue = defaultText ?? ""
        alert.accessoryView = input
        alert.window.initialFirstResponder = input
        let resp = alert.runModal()
        completionHandler(resp == .alertFirstButtonReturn ? input.stringValue : nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
