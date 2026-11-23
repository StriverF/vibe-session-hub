import AppKit
import CryptoKit
import Darwin
import Foundation
import WebKit

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

private func resolvePreferencesPort(stateDir: URL) -> Int? {
    let prefsURL = stateDir.appendingPathComponent("preferences.json")
    if let data = try? Data(contentsOf: prefsURL),
       let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
       let prefPort = parseValidPort(json["port"]) {
        return prefPort
    }
    return nil
}

private func resolveHubPort(stateDir: URL) -> Int {
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
    if let prefPort = resolvePreferencesPort(stateDir: stateDir) {
        return prefPort
    }

    // 4. Info.plist default override: VibeHubPort
    if let plistPort = parseValidPort(Bundle.main.object(forInfoDictionaryKey: "VibeHubPort")) {
        return plistPort
    }

    return defaultHubPort
}

private func escapeHTMLText(_ raw: String) -> String {
    return raw
        .replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
        .replacingOccurrences(of: "\"", with: "&quot;")
}

private func runCommand(_ executable: String, arguments: [String]) -> (status: Int32, stdout: String) {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: executable)
    proc.arguments = arguments
    let outPipe = Pipe()
    proc.standardOutput = outPipe
    proc.standardError = FileHandle.nullDevice
    do {
        try proc.run()
    } catch {
        return (-1, "")
    }
    let data = outPipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    let output = String(data: data, encoding: .utf8) ?? ""
    return (proc.terminationStatus, output)
}

private enum LaunchAgentError: LocalizedError {
    case serializationFailed
    case bootstrapFailed(Int32)

    var errorDescription: String? {
        switch self {
        case .serializationFailed:
            return "无法序列化 LaunchAgent 配置。"
        case .bootstrapFailed(let status):
            return "launchctl bootstrap 返回状态码 \(status)。"
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    private var backendProcess: Process?
    private let backendProcessLock = NSLock()
    var syncTimer: Timer?
    private let syncQueue = DispatchQueue(label: "com.vibesessionhub.icloud-bridge", qos: .utility)
    private let lifecycleQueue = DispatchQueue(label: "com.vibesessionhub.lifecycle", qos: .userInitiated)
    private var backendReady = false
    private var pendingOpenURL: URL?
    private var updater: AppUpdater!

    let hubPort: Int
    let hubURL: URL
    let stateDir: URL
    let icloudSyncDir: URL

    override init() {
        let resolvedState = resolveStateDir()
        self.stateDir = resolvedState
        self.hubPort = resolveHubPort(stateDir: resolvedState)
        self.hubURL = URL(string: "http://127.0.0.1:\(self.hubPort)")!
        self.icloudSyncDir = resolveICloudSyncDir()
        super.init()
    }

    private func endpointURL(_ pathWithQuery: String) -> URL {
        return URL(string: "http://127.0.0.1:\(hubPort)\(pathWithQuery)")!
    }

    private func appBundleVersion() -> String {
        let raw = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "1.0.0"
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "1.0.0" : trimmed
    }

    private func userLaunchAgentPlistURL() -> URL {
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")
    }

    private func autoUpgradeAllowsLaunchAgent() -> Bool {
        let env = ProcessInfo.processInfo.environment
        if let customState = env["VIBE_HUB_STATE_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !customState.isEmpty,
           env["VIBE_HUB_ALLOW_LAUNCHAGENT"] != "1" {
            return false
        }
        return true
    }

    private func resolveConfiguredFallbackPort() -> Int {
        return resolvePreferencesPort(stateDir: stateDir) ?? defaultHubPort
    }

    private func readPlistDict(at url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url),
              let raw = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil),
              let dict = raw as? [String: Any] else {
            return nil
        }
        return dict
    }

    private func resolveLaunchAgentPlistURLIfManagingCurrentPort() -> URL? {
        let plistURL = userLaunchAgentPlistURL()
        guard FileManager.default.fileExists(atPath: plistURL.path),
              let dict = readPlistDict(at: plistURL) else {
            return nil
        }
        let configuredPort = extractLaunchAgentConfiguredPort(
            plistDict: dict,
            fallbackPort: resolveConfiguredFallbackPort()
        )
        return configuredPort == hubPort ? plistURL : nil
    }

    private func isLaunchAgentLoaded() -> Bool {
        let uid = String(getuid())
        let res = runCommand("/bin/launchctl", arguments: ["print", "gui/\(uid)/\(launchAgentLabel)"])
        return res.status == 0
    }

    private func bootoutLaunchAgent(plistURL: URL) {
        let uid = String(getuid())
        _ = runCommand("/bin/launchctl", arguments: ["bootout", "gui/\(uid)/\(launchAgentLabel)"])
        if isLaunchAgentLoaded() && FileManager.default.fileExists(atPath: plistURL.path) {
            _ = runCommand("/bin/launchctl", arguments: ["bootout", "gui/\(uid)", plistURL.path])
        }
    }

    private func waitForLaunchAgentUnloaded(timeoutSec: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSec)
        while Date() < deadline {
            if !isLaunchAgentLoaded() {
                return true
            }
            Thread.sleep(forTimeInterval: 0.15)
        }
        return !isLaunchAgentLoaded()
    }

    private func waitForPortState(expectListening: Bool, timeoutSec: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSec)
        while Date() < deadline {
            let responding = fetchHealthSync(timeout: 0.4) != nil
            if responding == expectListening {
                return true
            }
            Thread.sleep(forTimeInterval: 0.15)
        }
        return (fetchHealthSync(timeout: 0.4) != nil) == expectListening
    }

    private func terminateLocalFallbackBackend() {
        backendProcessLock.lock()
        let proc = backendProcess
        backendProcess = nil
        backendProcessLock.unlock()

        if let proc = proc, proc.isRunning {
            proc.terminate()
            let deadline = Date().addingTimeInterval(1.5)
            while proc.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupUpdater()
        setupMainMenu()
        setupWindow()
        applyBundleDockIcon()
        ensureBackendAndLoad()
        startICloudBridgeTimer()
        updater.startPeriodicChecks()
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
            if window != nil {
                window.makeKeyAndOrderFront(nil)
            }
            if backendReady && webView != nil {
                loadHubPage(url: target, clearCache: false)
            } else {
                pendingOpenURL = target
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        updater?.handleAppWillTerminate()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "vibeHubUpdater")
        terminateLocalFallbackBackend()
    }

    private func setupUpdater() {
        let appUpdater = AppUpdater(
            currentVersion: appBundleVersion(),
            stateDir: stateDir
        )
        appUpdater.onStateChange = { [weak self] snapshot in
            self?.pushUpdaterStateToWebView(snapshot)
        }
        appUpdater.onRequestTerminate = {
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
        }
        self.updater = appUpdater
    }

    private func isLocalHubURL(_ url: URL?) -> Bool {
        guard let url = url,
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host?.lowercased(),
              host == "127.0.0.1" || host == "localhost" else {
            return false
        }
        let port = url.port ?? (scheme == "https" ? 443 : 80)
        return port == hubPort
    }

    private func pushUpdaterStateToWebView(_ snapshot: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: snapshot, options: []),
              var jsonStr = String(data: data, encoding: .utf8) else {
            return
        }
        jsonStr = jsonStr
            .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
            .replacingOccurrences(of: "\u{2029}", with: "\\u2029")

        DispatchQueue.main.async { [weak self] in
            guard let self = self,
                  self.webView != nil,
                  self.isLocalHubURL(self.webView.url) else {
                return
            }
            let js = "window.__vibeHubReceiveUpdateState && window.__vibeHubReceiveUpdateState(\(jsonStr));"
            self.webView.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    @objc private func checkForUpdatesMenuAction() {
        if window != nil {
            window.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
        updater?.checkNow()
        if webView != nil && isLocalHubURL(webView.url) {
            webView.evaluateJavaScript("window.__vibeHubOpenUpdateModal && window.__vibeHubOpenUpdateModal();", completionHandler: nil)
        }
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()
        let appVersion = appBundleVersion()

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
        appMenu.addItem(withTitle: "检查更新…", action: #selector(checkForUpdatesMenuAction), keyEquivalent: "")
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
        loadHubPage(url: hubURL, clearCache: true)
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

    private func showLoadingScreen(subtitle: String) {
        guard webView != nil else { return }
        let html = """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8" />
          <style>
            body {
              margin: 0; height: 100vh; display: grid; place-items: center;
              background: #0b0c10; color: #f3f4f8;
              font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
            }
            .box { text-align: center; max-width: 440px; padding: 24px; }
            .ring {
              width: 34px; height: 34px; margin: 0 auto 18px; border-radius: 50%;
              border: 3px solid rgba(139, 92, 246, 0.22);
              border-top-color: #8b5cf6;
              animation: spin 0.9s linear infinite;
            }
            h1 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
            p { font-size: 13px; color: #9ea3b8; margin: 0; line-height: 1.5; }
            @keyframes spin { to { transform: rotate(360deg); } }
          </style>
        </head>
        <body>
          <div class="box">
            <div class="ring"></div>
            <h1>Vibe Session Hub</h1>
            <p>\(escapeHTMLText(subtitle))</p>
          </div>
        </body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func loadHubPage(url: URL, clearCache: Bool) {
        guard webView != nil else { return }
        let req = URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 15
        )
        if clearCache {
            let cacheTypes: Set<String> = [
                WKWebsiteDataTypeDiskCache,
                WKWebsiteDataTypeMemoryCache,
            ]
            WKWebsiteDataStore.default().removeData(ofTypes: cacheTypes, modifiedSince: .distantPast) { [weak self] in
                DispatchQueue.main.async {
                    self?.webView.load(req)
                }
            }
        } else {
            webView.load(req)
        }
    }

    private func finishLoadingBackend(clearCache: Bool) {
        backendReady = true
        let target = pendingOpenURL ?? hubURL
        pendingOpenURL = nil
        loadHubPage(url: target, clearCache: clearCache)
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

    private func writeAndReloadLaunchAgent(
        backendRoot: URL,
        expectedRunningPid: Int?
    ) -> Result<Void, Error> {
        let fm = FileManager.default
        let plistURL = userLaunchAgentPlistURL()
        let agentsDir = plistURL.deletingLastPathComponent()
        try? fm.createDirectory(at: agentsDir, withIntermediateDirectories: true)
        try? fm.createDirectory(at: stateDir, withIntermediateDirectories: true)

        let plistDict = buildLaunchAgentPlist(
            label: launchAgentLabel,
            backendRoot: backendRoot,
            hubPort: hubPort,
            stateDir: stateDir,
            sendURLHelperPath: resolveSendURLHelperPath()
        )

        let plistData: Data
        do {
            plistData = try PropertyListSerialization.data(fromPropertyList: plistDict, format: .xml, options: 0)
        } catch {
            return .failure(error)
        }

        // Stop any local fallback backend spawned by this app and unload existing LaunchAgent cleanly first
        terminateLocalFallbackBackend()
        bootoutLaunchAgent(plistURL: plistURL)
        _ = waitForLaunchAgentUnloaded(timeoutSec: 2.5)
        _ = waitForPortState(expectListening: false, timeoutSec: 2.5)

        // If an unmanaged stale Vibe Session Hub process still holds the port, stop it cleanly before bootstrap
        if let lingering = fetchHealthSync(timeout: 0.4), isVibeHubHealthPayload(lingering) {
            let lingeringMeta = extractHealthServerMetadata(lingering)
            _ = stopUnmanagedBackendOnPort(expectedPid: lingeringMeta.pid ?? expectedRunningPid)
        }

        do {
            try plistData.write(to: plistURL, options: .atomic)
        } catch {
            return .failure(error)
        }

        let uid = String(getuid())
        var lastStatus: Int32 = -1
        for attempt in 0..<3 {
            let res = runCommand("/bin/launchctl", arguments: ["bootstrap", "gui/\(uid)", plistURL.path])
            lastStatus = res.status
            if lastStatus == 0 {
                return .success(())
            }
            if attempt < 2 {
                Thread.sleep(forTimeInterval: 0.3)
            }
        }
        return .failure(LaunchAgentError.bootstrapFailed(lastStatus))
    }

    @objc private func installLaunchAgentMenuAction() {
        guard let backendRoot = resolveBackendRoot() else {
            showInfoAlert(title: "无法定位内置服务目录", message: "请重新从 DMG 安装 Vibe Session Hub.app。")
            return
        }
        if isEphemeralBundleLocation(Bundle.main.bundleURL) {
            showInfoAlert(
                title: "请先安装到「应用程序」文件夹",
                message: "当前应用正在只读镜像（DMG）或临时路径中运行，无法注册开机后台服务。请先将「Vibe Session Hub.app」拖入「应用程序」文件夹后再打开。"
            )
            return
        }

        let expected = readExpectedBackendIdentity(
            backendRoot: backendRoot,
            bundleVersion: appBundleVersion()
        )
        showLoadingScreen(subtitle: "正在更新并重启开机后台服务 v\(expected.version)…")

        lifecycleQueue.async { [weak self] in
            guard let self = self else { return }
            let result = self.writeAndReloadLaunchAgent(backendRoot: backendRoot, expectedRunningPid: nil)
            switch result {
            case .success:
                DispatchQueue.main.async {
                    self.showInfoAlert(
                        title: "已启用开机后台服务",
                        message: "后台服务已更新至 v\(expected.version) 并监听 http://127.0.0.1:\(self.hubPort)，即使关闭主窗口也能随时响应跨机跳转链接与定时增量同步。"
                    )
                }
                self.pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: true)
            case .failure(let error):
                DispatchQueue.main.async {
                    self.showInfoAlert(
                        title: "注册后台服务失败",
                        message: "\(error.localizedDescription) 应用将继续使用内置后台服务运行。"
                    )
                }
                self.spawnFallbackBackend()
                self.pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: true)
            }
        }
    }

    @objc private func uninstallLaunchAgentMenuAction() {
        let plistURL = userLaunchAgentPlistURL()
        let backendRoot = resolveBackendRoot()
        let expected = backendRoot.map {
            readExpectedBackendIdentity(backendRoot: $0, bundleVersion: appBundleVersion())
        }

        lifecycleQueue.async { [weak self] in
            guard let self = self else { return }
            self.bootoutLaunchAgent(plistURL: plistURL)
            _ = self.waitForLaunchAgentUnloaded(timeoutSec: 2.5)
            try? FileManager.default.removeItem(at: plistURL)
            _ = self.waitForPortState(expectListening: false, timeoutSec: 2.0)

            // Keep the currently open desktop window working seamlessly via the bundled fallback backend
            if self.fetchHealthSync(timeout: 0.4) == nil {
                self.spawnFallbackBackend()
                self.pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: false)
            }

            DispatchQueue.main.async {
                self.showInfoAlert(
                    title: "已卸载开机后台服务",
                    message: "已移除 \(plistURL.lastPathComponent)。当前及后续打开应用时会自动使用内置本地服务运行。"
                )
            }
        }
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
        let contentController = WKUserContentController()
        let nativeFlagScript = WKUserScript(
            source: "window.__VIBE_HUB_NATIVE__ = true;",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        contentController.addUserScript(nativeFlagScript)
        contentController.add(self, name: "vibeHubUpdater")
        cfg.userContentController = contentController

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
        lifecycleQueue.async { [weak self] in
            guard let self = self else { return }
            guard let backendRoot = self.resolveBackendRoot() else {
                DispatchQueue.main.async {
                    self.finishLoadingBackend(clearCache: false)
                }
                return
            }

            let expected = readExpectedBackendIdentity(
                backendRoot: backendRoot,
                bundleVersion: self.appBundleVersion()
            )

            if let health = self.fetchHealthSync(timeout: 1.2) {
                guard isVibeHubHealthPayload(health) else {
                    DispatchQueue.main.async {
                        self.finishLoadingBackend(clearCache: false)
                    }
                    return
                }

                switch evaluateRunningBackend(health: health, expected: expected) {
                case .current:
                    DispatchQueue.main.async {
                        self.finishLoadingBackend(clearCache: false)
                    }
                    self.syncICloudBridgeAsync()
                    return

                case .newerRunning(let runningVer):
                    NSLog("VibeSessionHub: keeping newer running backend v%@ (bundle is v%@)", runningVer, expected.version)
                    DispatchQueue.main.async {
                        self.finishLoadingBackend(clearCache: false)
                    }
                    self.syncICloudBridgeAsync()
                    return

                case .stale(let reason):
                    NSLog(
                        "VibeSessionHub: stale backend detected (%@); upgrading to v%@ at %@",
                        reason,
                        expected.version,
                        expected.backendRoot
                    )
                    let runningMeta = extractHealthServerMetadata(health)
                    self.upgradeStaleBackend(
                        backendRoot: backendRoot,
                        expected: expected,
                        runningPid: runningMeta.pid
                    )
                    return
                }
            }

            // Nothing is responding on hubPort yet
            DispatchQueue.main.async {
                self.showLoadingScreen(subtitle: "正在启动 Vibe Session Hub v\(expected.version) 本地服务…")
            }

            if self.autoUpgradeAllowsLaunchAgent(),
               self.isLaunchAgentLoaded(),
               let plistURL = self.resolveLaunchAgentPlistURLIfManagingCurrentPort(),
               let plistDict = self.readPlistDict(at: plistURL) {
                if extractLaunchAgentWorkingDirectory(plistDict: plistDict) != expected.backendRoot {
                    if !isEphemeralBundleLocation(Bundle.main.bundleURL) {
                        NSLog("VibeSessionHub: repairing outdated LaunchAgent plist at %@", plistURL.path)
                        if case .success = self.writeAndReloadLaunchAgent(
                            backendRoot: backendRoot,
                            expectedRunningPid: nil
                        ) {
                            self.pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: true)
                            return
                        }
                    }
                } else {
                    if self.waitForPortState(expectListening: true, timeoutSec: 2.5) {
                        self.pollUntilReady(attemptsLeft: 20, expected: expected, clearCacheOnLoad: false)
                        return
                    }
                }
            }

            self.spawnFallbackBackend()
            self.pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: false)
        }
    }

    private func upgradeStaleBackend(
        backendRoot: URL,
        expected: ExpectedBackendIdentity,
        runningPid: Int?
    ) {
        let allowLA = autoUpgradeAllowsLaunchAgent()
        let laLoaded = allowLA && isLaunchAgentLoaded()
        let plistExists = FileManager.default.fileExists(atPath: userLaunchAgentPlistURL().path)
        let plistManagesPort = laLoaded && (resolveLaunchAgentPlistURLIfManagingCurrentPort() != nil)
        let orphanLoadedLA = laLoaded && !plistExists && hubPort == resolveConfiguredFallbackPort()

        if plistManagesPort && isEphemeralBundleLocation(Bundle.main.bundleURL) {
            NSLog("VibeSessionHub: skipping LaunchAgent auto-upgrade from read-only/ephemeral bundle location")
            DispatchQueue.main.async {
                self.finishLoadingBackend(clearCache: false)
                self.showInfoAlert(
                    title: "检测到旧版开机后台服务",
                    message: "当前应用正在只读镜像（DMG）或临时路径中运行，为避免后台服务失效未自动切换。\n\n请先退出应用，将「Vibe Session Hub.app」拖入「应用程序」文件夹后再打开，即可自动升级至 v\(expected.version)。"
                )
            }
            return
        }

        DispatchQueue.main.async {
            self.showLoadingScreen(subtitle: "检测到旧版后台服务，正在自动升级至 v\(expected.version)…")
        }

        if plistManagesPort {
            switch writeAndReloadLaunchAgent(backendRoot: backendRoot, expectedRunningPid: runningPid) {
            case .success:
                pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: true)
                return
            case .failure(let err):
                NSLog("VibeSessionHub: LaunchAgent auto-upgrade failed (%@), falling back to bundled backend", err.localizedDescription)
            }
        } else if orphanLoadedLA {
            bootoutLaunchAgent(plistURL: userLaunchAgentPlistURL())
            _ = waitForLaunchAgentUnloaded(timeoutSec: 2.5)
        }

        _ = stopUnmanagedBackendOnPort(expectedPid: runningPid)
        spawnFallbackBackend()
        pollUntilReady(attemptsLeft: 60, expected: expected, clearCacheOnLoad: true)
    }

    private func requestGracefulShutdownSync() -> Bool {
        var req = URLRequest(
            url: endpointURL("/api/shutdown"),
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 1.5
        )
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("1", forHTTPHeaderField: "X-Vibe-Hub")
        req.setValue("http://127.0.0.1:\(hubPort)", forHTTPHeaderField: "Origin")
        req.httpBody = Data("{}".utf8)

        let sem = DispatchSemaphore(value: 0)
        var accepted = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            defer { sem.signal() }
            if let httpResp = resp as? HTTPURLResponse, httpResp.statusCode == 200 {
                accepted = true
            }
        }.resume()
        _ = sem.wait(timeout: .now() + 2.0)
        return accepted
    }

    private func verifiedVibeHubListeningPids(expectedPid: Int?) -> [pid_t] {
        var candidateSet = Set<pid_t>()
        let lsofRes = runCommand(
            "/usr/sbin/lsof",
            arguments: ["-nP", "-iTCP:\(hubPort)", "-sTCP:LISTEN", "-t"]
        )
        if lsofRes.status == 0 {
            for line in lsofRes.stdout.split(whereSeparator: { $0.isNewline }) {
                if let pid = pid_t(line.trimmingCharacters(in: .whitespaces)), pid > 0 {
                    candidateSet.insert(pid)
                }
            }
        }
        if let hint = expectedPid, hint > 0 {
            candidateSet.insert(pid_t(hint))
        }

        let currentUid = getuid()
        let selfPid = getpid()
        var verified: [pid_t] = []
        for pid in candidateSet where pid != selfPid {
            let psRes = runCommand("/bin/ps", arguments: ["-p", String(pid), "-o", "uid=,command="])
            guard psRes.status == 0,
                  let parsed = parsePsUidAndCommand(psRes.stdout),
                  parsed.uid == currentUid,
                  isVibeHubServerCommand(parsed.command) else {
                continue
            }
            verified.append(pid)
        }
        return verified
    }

    private func stopUnmanagedBackendOnPort(expectedPid: Int?) -> Bool {
        // 1. Try graceful same-origin HTTP shutdown (supported by v1.0.2+ backends)
        if requestGracefulShutdownSync() {
            if waitForPortState(expectListening: false, timeoutSec: 2.5) {
                return true
            }
        }

        // 2. Fallback for older backends without /api/shutdown: strictly verified SIGTERM
        let pids = verifiedVibeHubListeningPids(expectedPid: expectedPid)
        guard !pids.isEmpty else {
            return waitForPortState(expectListening: false, timeoutSec: 1.0)
        }

        for pid in pids {
            Darwin.kill(pid, SIGTERM)
        }
        if waitForPortState(expectListening: false, timeoutSec: 3.0) {
            return true
        }

        // 3. Re-verify still matching Vibe Hub server before last-resort SIGKILL
        let stubborn = verifiedVibeHubListeningPids(expectedPid: expectedPid)
        for pid in stubborn {
            Darwin.kill(pid, SIGKILL)
        }
        return waitForPortState(expectListening: false, timeoutSec: 1.5)
    }

    private func fetchHealthSync(timeout: TimeInterval) -> [String: Any]? {
        var req = URLRequest(
            url: endpointURL("/api/health"),
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: timeout
        )
        req.httpMethod = "GET"
        let sem = DispatchSemaphore(value: 0)
        var result: [String: Any]?
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            defer { sem.signal() }
            guard let httpResp = resp as? HTTPURLResponse,
                  httpResp.statusCode == 200,
                  let data = data,
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                return
            }
            result = json
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.3)
        return result
    }

    private func fetchSyncHealth(completion: @escaping ([String: Any]?) -> Void) {
        var req = URLRequest(
            url: endpointURL("/api/health"),
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 1.2
        )
        req.httpMethod = "GET"
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
        backendProcessLock.lock()
        backendProcess = proc
        backendProcessLock.unlock()
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
                    req.setValue("http://127.0.0.1:\(self.hubPort)", forHTTPHeaderField: "Origin")
                    req.httpBody = Data("{\"skip_icloud\":true,\"bridge_synced\":true}".utf8)
                    URLSession.shared.dataTask(with: req).resume()
                }
            }
        }
    }

    private func pollUntilReady(
        attemptsLeft: Int,
        expected: ExpectedBackendIdentity?,
        clearCacheOnLoad: Bool
    ) {
        if attemptsLeft <= 0 {
            DispatchQueue.main.async {
                self.finishLoadingBackend(clearCache: clearCacheOnLoad)
            }
            self.syncICloudBridgeAsync()
            return
        }
        lifecycleQueue.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            guard let self = self else { return }
            if let health = self.fetchHealthSync(timeout: 0.8), isVibeHubHealthPayload(health) {
                if let expected = expected {
                    switch evaluateRunningBackend(health: health, expected: expected) {
                    case .current, .newerRunning:
                        DispatchQueue.main.async {
                            self.finishLoadingBackend(clearCache: clearCacheOnLoad)
                        }
                        self.syncICloudBridgeAsync()
                        return
                    case .stale:
                        break
                    }
                } else {
                    DispatchQueue.main.async {
                        self.finishLoadingBackend(clearCache: clearCacheOnLoad)
                    }
                    self.syncICloudBridgeAsync()
                    return
                }
            }
            self.pollUntilReady(
                attemptsLeft: attemptsLeft - 1,
                expected: expected,
                clearCacheOnLoad: clearCacheOnLoad
            )
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isLocalHubURL(webView.url), let updater = updater else { return }
        pushUpdaterStateToWebView(updater.snapshotDictionary())
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "vibeHubUpdater",
              message.frameInfo.isMainFrame else {
            return
        }
        let origin = message.frameInfo.securityOrigin
        let originHost = origin.host.lowercased()
        guard (originHost == "127.0.0.1" || originHost == "localhost"),
              origin.port == hubPort else {
            return
        }
        guard let body = message.body as? [String: Any],
              let action = body["action"] as? String,
              let updater = updater else {
            return
        }

        switch action {
        case "getState":
            pushUpdaterStateToWebView(updater.snapshotDictionary())
        case "check":
            updater.checkNow()
        case "start":
            updater.startUpdate()
        case "cancel":
            updater.cancelUpdate()
        case "restart":
            updater.restartUpdate()
        case "dismissJustUpdated":
            updater.dismissJustUpdated()
        case "openReleasePage":
            updater.openReleasePage()
        case "openDownloadURL":
            updater.openDownloadURL()
        default:
            break
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

@main
enum VibeSessionHubMain {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}
