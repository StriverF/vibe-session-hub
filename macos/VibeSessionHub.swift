import AppKit
import CryptoKit
import Darwin
import Foundation
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backendProcess: Process?
    var syncTimer: Timer?
    private let syncQueue = DispatchQueue(label: "com.vibesessionhub.icloud-bridge", qos: .utility)
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
                self.syncICloudBridgeAsync()
                return
            }
            self.spawnFallbackBackend()
            self.pollUntilReady(attemptsLeft: 25)
        }
    }

    private func fetchSyncHealth(completion: @escaping ([String: Any]?) -> Void) {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:17380/api/health")!)
        req.timeoutInterval = 1.2
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            guard let httpResp = resp as? HTTPURLResponse,
                  httpResp.statusCode == 200,
                  let data = data,
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                completion(nil)
                return
            }
            completion(json)
        }.resume()
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

    private func sha256Hex(_ data: Data) -> String {
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private func startICloudBridgeTimer() {
        syncTimer = Timer.scheduledTimer(withTimeInterval: 60.0, repeats: true) { [weak self] _ in
            self?.syncICloudBridgeAsync()
        }
    }

    private func syncICloudBridgeAsync() {
        fetchSyncHealth { [weak self] health in
            guard let self = self, let health = health else { return }
            if (health["local_only"] as? Bool) == true {
                return
            }
            // Only act as iCloud bridge when backend explicitly delegates ownership (e.g. LaunchAgent TCC denial).
            guard (health["sync_owner"] as? String) == "app_bridge" else {
                return
            }
            guard let machine = health["machine"] as? [String: Any],
                  let machineId = machine["machine_id"] as? String,
                  !machineId.isEmpty else {
                return
            }

            self.syncQueue.async {
                let fm = FileManager.default
                let home = fm.homeDirectoryForCurrentUser
                let localSync = home.appendingPathComponent(".vibe-session-hub/sync")
                let icloudRoot = home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
                let icloudBase = icloudRoot.appendingPathComponent("VibeSessionHub")
                guard fm.fileExists(atPath: icloudRoot.path) else {
                    return
                }

                try? fm.createDirectory(at: localSync, withIntermediateDirectories: true)
                let lockPath = localSync.appendingPathComponent(".sync.lock").path
                // On macOS/BSD, O_EXLOCK | O_NONBLOCK atomically acquires a BSD flock(LOCK_EX | LOCK_NB)
                // compatible with Python's fcntl.flock() and automatically releases on close(fd).
                let fd = Darwin.open(lockPath, O_CREAT | O_RDWR | O_EXLOCK | O_NONBLOCK, S_IRUSR | S_IWUSR)
                guard fd >= 0 else { return }
                defer { Darwin.close(fd) }

                let backendNeedsBridgeStatus = (health["icloud_status"] as? String) != "synced_via_app"
                var pulledRemoteChange = false
                var bridgeAccessible = false
                let scopes: [(sub: String, ownFile: String)] = [
                    ("nodes", "\(machineId).jsonl"),
                    ("manual_links", "\(machineId)-links.jsonl"),
                ]

                for scope in scopes {
                    let lDir = localSync.appendingPathComponent(scope.sub)
                    let cDir = icloudBase.appendingPathComponent(scope.sub)
                    try? fm.createDirectory(at: lDir, withIntermediateDirectories: true)
                    do {
                        try fm.createDirectory(at: cDir, withIntermediateDirectories: true)
                        bridgeAccessible = true
                    } catch {
                        continue
                    }

                    // Trigger download of any evicted .icloud placeholder files
                    if let cloudItems = try? fm.contentsOfDirectory(at: cDir, includingPropertiesForKeys: nil) {
                        for item in cloudItems where item.lastPathComponent.hasPrefix(".") && item.lastPathComponent.hasSuffix(".icloud") {
                            try? fm.startDownloadingUbiquitousItem(at: item)
                        }
                    }

                    // PUSH strictly this machine's own shard (content-hashed + atomic write)
                    let localOwn = lDir.appendingPathComponent(scope.ownFile)
                    let cloudOwn = cDir.appendingPathComponent(scope.ownFile)
                    if fm.fileExists(atPath: localOwn.path),
                       let localData = try? Data(contentsOf: localOwn) {
                        let localHash = self.sha256Hex(localData)
                        let cloudHash = (try? Data(contentsOf: cloudOwn)).map { self.sha256Hex($0) }
                        if cloudHash != localHash {
                            try? localData.write(to: cloudOwn, options: .atomic)
                        }
                    }

                    // PULL strictly remote machines' shards (content-hashed + atomic write)
                    if let cFiles = try? fm.contentsOfDirectory(at: cDir, includingPropertiesForKeys: nil) {
                        for cf in cFiles where cf.pathExtension == "jsonl"
                            && !cf.lastPathComponent.hasPrefix(".")
                            && cf.lastPathComponent != scope.ownFile {
                            let lf = lDir.appendingPathComponent(cf.lastPathComponent)
                            if let remoteData = try? Data(contentsOf: cf) {
                                let remoteHash = self.sha256Hex(remoteData)
                                let localHash = (try? Data(contentsOf: lf)).map { self.sha256Hex($0) }
                                if localHash != remoteHash {
                                    try? remoteData.write(to: lf, options: .atomic)
                                    pulledRemoteChange = true
                                }
                            }
                        }
                    }
                }

                if bridgeAccessible && (pulledRemoteChange || backendNeedsBridgeStatus) {
                    var req = URLRequest(url: URL(string: "http://127.0.0.1:17380/api/sync")!)
                    req.httpMethod = "POST"
                    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    req.setValue("1", forHTTPHeaderField: "X-Vibe-Hub")
                    req.httpBody = Data("{\"skip_icloud\":true,\"bridge_synced\":true}".utf8)
                    URLSession.shared.dataTask(with: req).resume()
                }
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
                    self.syncICloudBridgeAsync()
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
