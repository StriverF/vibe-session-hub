import AppKit
import CryptoKit
import Darwin
import Foundation
import WebKit

private let defaultHubPort = 17380
private let launchAgentLabel = "com.vibesessionhub.app"

private func parseValidPort(_ raw: Any?) -> Int? {
    if let intVal = raw as? Int, (1...65535).contains(intVal) {
        return intVal
    }
    if let numVal = raw as? NSNumber {
        let intVal = numVal.intValue
        if (1...65535).contains(intVal) {
            return intVal
        }
    }
    if let strVal = raw as? String {
        let trimmed = strVal.trimmingCharacters(in: .whitespacesAndNewlines)
        if let intVal = Int(trimmed), (1...65535).contains(intVal) {
            return intVal
        }
    }
    return nil
}

private func resolveStateDir() -> URL {
    let env = ProcessInfo.processInfo.environment
    if let custom = env["VIBE_HUB_STATE_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines),
       !custom.isEmpty {
        return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
    }
    return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".vibe-session-hub")
}

private func resolveICloudSyncDir() -> URL {
    let env = ProcessInfo.processInfo.environment
    if let custom = env["VIBE_HUB_SYNC_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines),
       !custom.isEmpty {
        return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub")
}

private func resolveHubPort() -> Int {
    // 1. CLI flag: --port <N> or --port=<N>
    let args = CommandLine.arguments
    for i in 0..<args.count {
        if args[i] == "--port", i + 1 < args.count, let port = parseValidPort(args[i + 1]) {
            return port
        }
        if args[i].hasPrefix("--port=") {
            let val = String(args[i].dropFirst("--port=".count))
            if let port = parseValidPort(val) {
                return port
            }
        }
    }

    // 2. Environment variable: VIBE_HUB_PORT
    if let envPort = parseValidPort(ProcessInfo.processInfo.environment["VIBE_HUB_PORT"]) {
        return envPort
    }

    // 3. User preferences file: ~/.vibe-session-hub/preferences.json -> "port"
    let prefsURL = resolveStateDir().appendingPathComponent("preferences.json")
    if let data = try? Data(contentsOf: prefsURL),
       let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
       let prefPort = parseValidPort(json["port"]) {
        return prefPort
    }

    // 4. Info.plist default override: VibeHubPort
    if let plistPort = parseValidPort(Bundle.main.object(forInfoDictionaryKey: "VibeHubPort")) {
        return plistPort
    }

    return defaultHubPort
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backendProcess: Process?
    var syncTimer: Timer?
    private let syncQueue = DispatchQueue(label: "com.vibesessionhub.icloud-bridge", qos: .utility)

    let hubPort: Int
    let hubURL: URL
    let stateDir: URL
    let icloudSyncDir: URL

    override init() {
        self.hubPort = resolveHubPort()
        self.hubURL = URL(string: "http://127.0.0.1:\(self.hubPort)")!
        self.stateDir = resolveStateDir()
        self.icloudSyncDir = resolveICloudSyncDir()
        super.init()
    }

    private func endpointURL(_ pathWithQuery: String) -> URL {
        return URL(string: "http://127.0.0.1:\(hubPort)\(pathWithQuery)")!
    }

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
            let target = endpointURL("/jump?\(query)")
            window.makeKeyAndOrderFront(nil)
            webView.load(URLRequest(url: target))
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let proc = backendProcess, proc.isRunning {
            proc.terminate()
        }
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()
        let appVersion = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "1.0.0"

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        let versionItem = NSMenuItem(
            title: "Vibe Session Hub v\(appVersion) (127.0.0.1:\(hubPort))",
            action: nil,
            keyEquivalent: ""
        )
        versionItem.isEnabled = false
        appMenu.addItem(versionItem)
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "重新加载", action: #selector(reloadPage), keyEquivalent: "r")
        appMenu.addItem(withTitle: "在浏览器中打开", action: #selector(openInBrowser), keyEquivalent: "b")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "安装 / 更新开机后台服务", action: #selector(installLaunchAgentMenuAction), keyEquivalent: "")
        appMenu.addItem(withTitle: "卸载开机后台服务", action: #selector(uninstallLaunchAgentMenuAction), keyEquivalent: "")
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

    @objc private func openInBrowser() {
        NSWorkspace.shared.open(hubURL)
    }

    @objc private func closeCurrentWindow() {
        window.orderOut(nil)
    }

    private func showInfoAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    private func resolveBackendRoot() -> URL? {
        let fm = FileManager.default
        guard let resURL = Bundle.main.resourceURL else { return nil }

        // 1. Dev checkout mode (written by install.sh): prefer local repo when present
        let rootPointer = resURL.appendingPathComponent("project_root.txt")
        if let projectRoot = try? String(contentsOf: rootPointer, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !projectRoot.isEmpty {
            let devURL = URL(fileURLWithPath: projectRoot)
            if fm.fileExists(atPath: devURL.appendingPathComponent("hub/server.py").path) {
                return devURL
            }
        }

        // 2. Standalone release bundle mode (DMG / Applications): embedded runtime inside .app
        let bundledAppURL = resURL.appendingPathComponent("app")
        if fm.fileExists(atPath: bundledAppURL.appendingPathComponent("hub/server.py").path) {
            return bundledAppURL
        }

        return nil
    }

    private func resolveSendURLHelperPath() -> String? {
        let candidate = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/send_url_to_pid")
        return FileManager.default.isExecutableFile(atPath: candidate.path) ? candidate.path : nil
    }

    @objc private func installLaunchAgentMenuAction() {
        guard let backendRoot = resolveBackendRoot() else {
            showInfoAlert(title: "无法定位内置服务目录", message: "请重新从 DMG 安装 Vibe Session Hub.app。")
            return
        }
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let agentsDir = home.appendingPathComponent("Library/LaunchAgents")
        let plistURL = agentsDir.appendingPathComponent("\(launchAgentLabel).plist")
        try? fm.createDirectory(at: agentsDir, withIntermediateDirectories: true)
        try? fm.createDirectory(at: stateDir, withIntermediateDirectories: true)

        var envVars: [String: String] = [
            "VIBE_HUB_PORT": String(hubPort),
            "PYTHONDONTWRITEBYTECODE": "1",
        ]
        if let helperPath = resolveSendURLHelperPath() {
            envVars["VIBE_HUB_SEND_URL_BIN"] = helperPath
        }

        let plistDict: [String: Any] = [
            "Label": launchAgentLabel,
            "ProgramArguments": [
                "/usr/bin/python3",
                "-B",
                "-m",
                "hub.server",
                "--port",
                String(hubPort),
            ],
            "WorkingDirectory": backendRoot.path,
            "EnvironmentVariables": envVars,
            "RunAtLoad": true,
            "KeepAlive": true,
            "StandardOutPath": stateDir.appendingPathComponent("server.log").path,
            "StandardErrorPath": stateDir.appendingPathComponent("server.err.log").path,
        ]

        do {
            let plistData = try PropertyListSerialization.data(fromPropertyList: plistDict, format: .xml, options: 0)
            try plistData.write(to: plistURL, options: .atomic)

            // Stop any local fallback backend spawned by this app so LaunchAgent owns the port cleanly
            if let proc = backendProcess, proc.isRunning {
                proc.terminate()
                backendProcess = nil
            }

            let uid = String(getuid())
            let bootout = Process()
            bootout.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            bootout.arguments = ["bootout", "gui/\(uid)", plistURL.path]
            try? bootout.run()
            bootout.waitUntilExit()

            let bootstrap = Process()
            bootstrap.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            bootstrap.arguments = ["bootstrap", "gui/\(uid)", plistURL.path]
            try bootstrap.run()
            bootstrap.waitUntilExit()

            if bootstrap.terminationStatus == 0 {
                showInfoAlert(
                    title: "已启用开机后台服务",
                    message: "后台服务将随系统启动并监听 http://127.0.0.1:\(hubPort)，即使关闭主窗口也能随时响应跨机跳转链接与定时增量同步。"
                )
                pollUntilReady(attemptsLeft: 20)
            } else {
                showInfoAlert(
                    title: "注册后台服务失败",
                    message: "launchctl 返回状态码 \(bootstrap.terminationStatus)，应用将继续使用内置后台服务运行。"
                )
            }
        } catch {
            showInfoAlert(title: "写入 LaunchAgent 失败", message: error.localizedDescription)
        }
    }

    @objc private func uninstallLaunchAgentMenuAction() {
        let fm = FileManager.default
        let plistURL = fm.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")
        let uid = String(getuid())

        let bootout = Process()
        bootout.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        bootout.arguments = ["bootout", "gui/\(uid)", plistURL.path]
        try? bootout.run()
        bootout.waitUntilExit()

        try? fm.removeItem(at: plistURL)
        showInfoAlert(
            title: "已卸载开机后台服务",
            message: "已移除 \(plistURL.lastPathComponent)。重新打开应用时仍会自动启动内置本地服务。"
        )
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
            self.pollUntilReady(attemptsLeft: 30)
        }
    }

    private func fetchSyncHealth(completion: @escaping ([String: Any]?) -> Void) {
        var req = URLRequest(url: endpointURL("/api/health"))
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
        var req = URLRequest(url: endpointURL("/api/health"))
        req.timeoutInterval = 0.6
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            completion(code == 200)
        }.resume()
    }

    private func spawnFallbackBackend() {
        guard let backendRoot = resolveBackendRoot() else { return }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = ["-B", "-m", "hub.server", "--port", String(hubPort)]
        proc.currentDirectoryURL = backendRoot

        var env = ProcessInfo.processInfo.environment
        env["VIBE_HUB_PORT"] = String(hubPort)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        if let helperPath = resolveSendURLHelperPath() {
            env["VIBE_HUB_SEND_URL_BIN"] = helperPath
        }
        proc.environment = env

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
                let localSync = self.stateDir.appendingPathComponent("sync")
                let icloudRoot = home.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
                let icloudBase = self.icloudSyncDir
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
                    var req = URLRequest(url: self.endpointURL("/api/sync"))
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
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "about" {
            decisionHandler(.allow)
            return
        }
        if scheme != "http" && scheme != "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        let host = url.host?.lowercased() ?? ""
        let port = url.port ?? (scheme == "https" ? 443 : 80)
        if (host != "127.0.0.1" && host != "localhost") || port != hubPort {
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
