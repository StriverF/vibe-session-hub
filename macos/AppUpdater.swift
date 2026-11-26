import AppKit
import CryptoKit
import Darwin
import Foundation

private enum SelfUpdateCapability {
    case supported(targetAppURL: URL)
    case unsupported(reason: String)
}

private enum AppUpdaterError: LocalizedError {
    case invalidFeedURL
    case httpError(Int)
    case invalidResponse
    case missingDmgAsset
    case untrustedRedirect(String)
    case downloadTooLarge
    case downloadStalled
    case cancelled
    case sha256Missing
    case sha256Conflict
    case sha256Mismatch
    case sizeMismatch(expected: Int64, actual: Int64)
    case mountFailed(String)
    case appBundleNotFoundInDmg
    case copyFailed(String)
    case invalidBundle(String)
    case codesignVerifyFailed(String)
    case teamIdentifierMismatch
    case helperSpawnFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidFeedURL:
            return "更新源地址无效。"
        case .httpError(let code):
            return "更新服务返回 HTTP \(code)。"
        case .invalidResponse:
            return "无法解析版本发布信息。"
        case .missingDmgAsset:
            return "该版本未附带 macOS DMG 安装包，请前往 GitHub 手动下载。"
        case .untrustedRedirect(let host):
            return "已拦截不受信任的重定向目标：\(host)。"
        case .downloadTooLarge:
            return "安装包体积超过安全上限，已中止下载。"
        case .downloadStalled:
            return "下载超时：网络连接已中断。"
        case .cancelled:
            return "已取消下载。"
        case .sha256Missing:
            return "该版本缺少 SHA-256 校验信息，已拒绝自动安装。"
        case .sha256Conflict:
            return "GitHub 摘要与 .sha256 校验文件不一致，已中止更新。"
        case .sha256Mismatch:
            return "安装包 SHA-256 完整性校验失败，请重试。"
        case .sizeMismatch(let expected, let actual):
            return "安装包大小校验失败（期望 \(expected) 字节，实际 \(actual) 字节）。"
        case .mountFailed(let detail):
            return "无法挂载 DMG 安装镜像：\(detail)"
        case .appBundleNotFoundInDmg:
            return "安装包中未找到有效的 Vibe Session Hub.app。"
        case .copyFailed(let detail):
            return "解压安装包失败：\(detail)"
        case .invalidBundle(let detail):
            return "新版本应用包校验未通过：\(detail)"
        case .codesignVerifyFailed(let detail):
            return "新版本签名完整性校验失败：\(detail)"
        case .teamIdentifierMismatch:
            return "新版本开发者签名团队与当前应用不一致，已拒绝自动替换。"
        case .helperSpawnFailed(let detail):
            return "无法启动更新替换进程：\(detail)"
        }
    }
}

private struct StagedBundleUpdate {
    let workDir: URL
    let stagedAppURL: URL
    let targetAppURL: URL
    let helperProcess: Process
}

private final class StreamingDmgDownloader: NSObject, URLSessionDataDelegate {
    private let destinationURL: URL
    private let allowLoopback: Bool
    private let maxBytes: Int64
    private let stallTimeoutSec: TimeInterval
    private let onProgress: (Int64, Int64, Int) -> Void

    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var fileHandle: FileHandle?
    private var hasher = SHA256()
    private var receivedBytes: Int64 = 0
    private var expectedBytes: Int64 = 0
    private var lastReportedPercent: Int = -1
    private var lastReportTime: Date = .distantPast
    private var lastActivityTime = Date()
    private var stallTimer: DispatchSourceTimer?

    private let lock = NSLock()
    private var cancelled = false
    private var customError: Error?
    private var completionResult: Result<(bytes: Int64, sha256: String), Error>?
    private let semaphore = DispatchSemaphore(value: 0)

    init(
        destinationURL: URL,
        expectedSize: Int64,
        allowLoopback: Bool,
        maxBytes: Int64 = maxUpdateArchiveBytes,
        stallTimeoutSec: TimeInterval = 60.0,
        onProgress: @escaping (Int64, Int64, Int) -> Void
    ) {
        self.destinationURL = destinationURL
        self.expectedBytes = expectedSize
        self.allowLoopback = allowLoopback
        self.maxBytes = maxBytes
        self.stallTimeoutSec = stallTimeoutSec
        self.onProgress = onProgress
        super.init()
    }

    func downloadSync(from url: URL, userAgent: String) -> Result<(bytes: Int64, sha256: String), Error> {
        let fm = FileManager.default
        fm.createFile(atPath: destinationURL.path, contents: nil)
        guard let fh = try? FileHandle(forWritingTo: destinationURL) else {
            return .failure(AppUpdaterError.copyFailed("无法创建临时下载文件"))
        }
        self.fileHandle = fh

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 3600
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData

        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        let urlSession = URLSession(configuration: config, delegate: self, delegateQueue: queue)
        self.session = urlSession

        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 30)
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")

        let dataTask = urlSession.dataTask(with: req)
        lock.lock()
        let alreadyCancelled = cancelled
        self.task = dataTask
        self.lastActivityTime = Date()
        lock.unlock()

        if alreadyCancelled {
            finish(with: .failure(AppUpdaterError.cancelled))
            return .failure(AppUpdaterError.cancelled)
        }

        startStallWatchdog()
        dataTask.resume()
        semaphore.wait()
        return completionResult ?? .failure(AppUpdaterError.invalidResponse)
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let activeTask = task
        lock.unlock()
        activeTask?.cancel()
    }

    private func startStallWatchdog() {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        timer.schedule(deadline: .now() + 5.0, repeating: 5.0)
        timer.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.lock.lock()
            let elapsed = Date().timeIntervalSince(self.lastActivityTime)
            let isDone = self.completionResult != nil
            if !isDone && elapsed >= self.stallTimeoutSec {
                self.customError = AppUpdaterError.downloadStalled
                let activeTask = self.task
                self.lock.unlock()
                activeTask?.cancel()
                return
            }
            self.lock.unlock()
        }
        self.stallTimer = timer
        timer.resume()
    }

    private func finish(with result: Result<(bytes: Int64, sha256: String), Error>) {
        lock.lock()
        if completionResult != nil {
            lock.unlock()
            return
        }
        completionResult = result
        let timer = stallTimer
        stallTimer = nil
        let fh = fileHandle
        fileHandle = nil
        let sess = session
        session = nil
        task = nil
        lock.unlock()

        timer?.cancel()
        try? fh?.close()
        sess?.invalidateAndCancel()
        semaphore.signal()
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let nextURL = request.url,
              isTrustedUpdateURL(nextURL.absoluteString, allowLoopback: allowLoopback) else {
            let badHost = request.url?.host ?? "unknown"
            lock.lock()
            customError = AppUpdaterError.untrustedRedirect(badHost)
            lock.unlock()
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let httpResp = response as? HTTPURLResponse else {
            lock.lock()
            customError = AppUpdaterError.invalidResponse
            lock.unlock()
            completionHandler(.cancel)
            return
        }
        guard httpResp.statusCode == 200 else {
            lock.lock()
            customError = AppUpdaterError.httpError(httpResp.statusCode)
            lock.unlock()
            completionHandler(.cancel)
            return
        }
        if httpResp.expectedContentLength > 0 {
            if httpResp.expectedContentLength > maxBytes {
                lock.lock()
                customError = AppUpdaterError.downloadTooLarge
                lock.unlock()
                completionHandler(.cancel)
                return
            }
            expectedBytes = httpResp.expectedContentLength
        }
        lock.lock()
        lastActivityTime = Date()
        lock.unlock()
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        lastActivityTime = Date()
        let isCancelled = cancelled
        lock.unlock()
        if isCancelled {
            dataTask.cancel()
            return
        }

        receivedBytes += Int64(data.count)
        if receivedBytes > maxBytes {
            lock.lock()
            customError = AppUpdaterError.downloadTooLarge
            lock.unlock()
            dataTask.cancel()
            return
        }

        do {
            try fileHandle?.write(contentsOf: data)
        } catch {
            lock.lock()
            customError = AppUpdaterError.copyFailed(error.localizedDescription)
            lock.unlock()
            dataTask.cancel()
            return
        }
        hasher.update(data: data)

        let total = max(expectedBytes, receivedBytes)
        let pct = total > 0 ? min(99, Int((Double(receivedBytes) / Double(total)) * 100.0)) : 0
        let now = Date()
        if pct != lastReportedPercent || now.timeIntervalSince(lastReportTime) >= 0.15 {
            lastReportedPercent = pct
            lastReportTime = now
            onProgress(receivedBytes, expectedBytes, pct)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let isCancelled = cancelled
        let overrideError = customError
        lock.unlock()

        if let overrideError = overrideError {
            finish(with: .failure(overrideError))
            return
        }
        if isCancelled {
            finish(with: .failure(AppUpdaterError.cancelled))
            return
        }
        if let error = error {
            let nsErr = error as NSError
            if nsErr.domain == NSURLErrorDomain && nsErr.code == NSURLErrorCancelled {
                finish(with: .failure(AppUpdaterError.cancelled))
            } else {
                finish(with: .failure(error))
            }
            return
        }
        let digestHex = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        finish(with: .success((bytes: receivedBytes, sha256: digestHex)))
    }
}

final class AppUpdater {
    static let restartMarkerFilename = "update-restart.json"
    static let lastUpdateFilename = "update-last.json"

    let currentVersion: String
    let stateDir: URL
    let feedURL: String
    let allowLoopbackFeed: Bool
    let cliPortOverride: Int?

    var onStateChange: (([String: Any]) -> Void)?
    var onRequestTerminate: (() -> Void)?

    private let capability: SelfUpdateCapability
    private let stateLock = NSLock()
    private let workQueue = DispatchQueue(label: "com.vibesessionhub.updater", qos: .utility)

    private var status: String = "idle" // idle | available | downloading | installing | ready | restarting | failed
    private var checking: Bool = false
    private var latestConfirmed: Bool = false
    private var percent: Int = 0
    private var downloadedBytes: Int64 = 0
    private var totalBytes: Int64 = 0
    private var errorText: String = ""
    private var checkErrorText: String = ""
    private var checkedAtMs: Int64 = 0
    private var foundUpdate: ReleaseUpdateInfo?
    private var justUpdated: JustUpdatedOutcome?
    private var updatedFrom: String = ""
    private var updatedNotes: String = ""
    private var updatedPublishedAt: String = ""

    private var activeDownloader: StreamingDmgDownloader?
    private var activeSidecarTask: URLSessionDataTask?
    private var activeWorkDir: URL?
    private var stagedUpdate: StagedBundleUpdate?
    private var periodicTimer: DispatchSourceTimer?
    private var checkInFlight: Bool = false
    private var cancelling: Bool = false

    private let initialCheckDelaySec: TimeInterval = 6.0
    private let periodicCheckIntervalSec: TimeInterval = 6.0 * 3600.0
    private let retryCheckIntervalSec: TimeInterval = 30.0 * 60.0

    init(
        currentVersion: String,
        stateDir: URL,
        bundleURL: URL = Bundle.main.bundleURL,
        resourceURL: URL? = Bundle.main.resourceURL,
        env: [String: String] = ProcessInfo.processInfo.environment,
        cliArguments: [String] = CommandLine.arguments
    ) {
        let stripped = currentVersion.trimmingCharacters(in: .whitespacesAndNewlines)
        self.currentVersion = (stripped.hasPrefix("v") || stripped.hasPrefix("V"))
            ? String(stripped.dropFirst())
            : (stripped.isEmpty ? "1.0.0" : stripped)
        self.stateDir = stateDir
        let feedCfg = resolveUpdateFeedConfig(env: env)
        self.feedURL = feedCfg.url
        self.allowLoopbackFeed = feedCfg.allowLoopback
        self.cliPortOverride = extractCliPortOverride(arguments: cliArguments)
        self.capability = AppUpdater.evaluateCapability(bundleURL: bundleURL, resourceURL: resourceURL)

        loadUpdateOutcome()
        cleanupOrphanedStagingDirsAsync()
    }

    var canSelfUpdate: Bool {
        if case .supported = capability { return true }
        return false
    }

    var cannotUpdateReason: String {
        if case .unsupported(let reason) = capability { return reason }
        return ""
    }

    private static func evaluateCapability(bundleURL: URL, resourceURL: URL?) -> SelfUpdateCapability {
        let fm = FileManager.default
        let resolvedBundle = bundleURL.standardizedFileURL.resolvingSymlinksInPath()
        guard resolvedBundle.pathExtension.lowercased() == "app" else {
            return .unsupported(reason: "not_app_bundle")
        }
        if let resURL = resourceURL,
           fm.fileExists(atPath: resURL.appendingPathComponent("project_root.txt").path) {
            return .unsupported(reason: "dev_checkout")
        }
        if isEphemeralBundleLocation(resolvedBundle) {
            return .unsupported(reason: "ephemeral_location")
        }

        let parentDir = resolvedBundle.deletingLastPathComponent()
        // If parent directory has sticky bit set and bundle is owned by a different user, mv will fail.
        if let parentAttrs = try? fm.attributesOfItem(atPath: parentDir.path),
           let parentPosix = parentAttrs[.posixPermissions] as? NSNumber,
           (parentPosix.uint16Value & 0o1000) != 0,
           let bundleAttrs = try? fm.attributesOfItem(atPath: resolvedBundle.path),
           let ownerID = bundleAttrs[.ownerAccountID] as? NSNumber,
           ownerID.uint32Value != getuid() {
            return .unsupported(reason: "not_writable")
        }

        let probeDir = parentDir.appendingPathComponent(".vibe-hub-probe-\(UUID().uuidString)")
        do {
            try fm.createDirectory(at: probeDir, withIntermediateDirectories: false)
            try? fm.removeItem(at: probeDir)
        } catch {
            return .unsupported(reason: "not_writable")
        }
        return .supported(targetAppURL: resolvedBundle)
    }

    private func cleanupOrphanedStagingDirsAsync() {
        guard case .supported(let targetApp) = capability else { return }
        let parentDir = targetApp.deletingLastPathComponent()
        let prefix = ".\(targetApp.lastPathComponent).update-"
        workQueue.async {
            let fm = FileManager.default
            guard let items = try? fm.contentsOfDirectory(
                at: parentDir,
                includingPropertiesForKeys: [.contentModificationDateKey],
                options: []
            ) else { return }
            let cutoff = Date().addingTimeInterval(-300)
            for item in items where item.lastPathComponent.hasPrefix(prefix) {
                let mtime = (try? item.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                if mtime < cutoff {
                    try? fm.removeItem(at: item)
                }
            }
        }
    }

    private func loadUpdateOutcome() {
        let fm = FileManager.default
        let markerURL = stateDir.appendingPathComponent(AppUpdater.restartMarkerFilename)
        let lastURL = stateDir.appendingPathComponent(AppUpdater.lastUpdateFilename)

        var loadedJust: JustUpdatedOutcome?
        if let data = try? Data(contentsOf: markerURL) {
            try? fm.removeItem(at: markerURL)
            if let marker = try? JSONDecoder().decode(UpdateRestartMarker.self, from: data),
               !marker.to.isEmpty {
                let ok = compareSemver(currentVersion, marker.to) >= 0
                loadedJust = JustUpdatedOutcome(
                    from: marker.from,
                    to: marker.to,
                    ok: ok,
                    notes: marker.notes ?? "",
                    publishedAt: marker.publishedAt ?? ""
                )
                NSLog("VibeSessionHub updater: requested v%@, running v%@ (ok=%@)", marker.to, currentVersion, ok ? "true" : "false")
                if ok, let encoded = try? JSONEncoder().encode(marker) {
                    try? fm.createDirectory(at: stateDir, withIntermediateDirectories: true)
                    try? encoded.write(to: lastURL, options: .atomic)
                }
            }
        }

        var loadedFrom = ""
        var loadedNotes = ""
        var loadedPublishedAt = ""
        if let data = try? Data(contentsOf: lastURL),
           let marker = try? JSONDecoder().decode(UpdateRestartMarker.self, from: data),
           compareSemver(currentVersion, marker.to) == 0 {
            loadedFrom = marker.from
            loadedNotes = marker.notes ?? ""
            loadedPublishedAt = marker.publishedAt ?? ""
        }

        stateLock.lock()
        justUpdated = loadedJust
        updatedFrom = loadedFrom
        updatedNotes = loadedNotes
        updatedPublishedAt = loadedPublishedAt
        stateLock.unlock()
    }

    private func writeUpdateMarker(from: String, to: String, notes: String = "", publishedAt: String = "") {
        let fm = FileManager.default
        try? fm.createDirectory(at: stateDir, withIntermediateDirectories: true)
        let marker = UpdateRestartMarker(
            from: from,
            to: to,
            notes: notes.isEmpty ? nil : notes,
            publishedAt: publishedAt.isEmpty ? nil : publishedAt
        )
        guard let data = try? JSONEncoder().encode(marker) else { return }
        let markerURL = stateDir.appendingPathComponent(AppUpdater.restartMarkerFilename)
        try? data.write(to: markerURL, options: .atomic)
    }

    func snapshotDictionary() -> [String: Any] {
        stateLock.lock()
        defer { stateLock.unlock() }

        var payload: [String: Any] = [
            "ok": true,
            "mode": "native",
            "current_version": currentVersion,
            "can_self_update": canSelfUpdate,
            "cannot_update_reason": cannotUpdateReason,
            "status": status,
            "checking": checking,
            "latest_confirmed": latestConfirmed,
            "percent": percent,
            "downloaded_bytes": downloadedBytes,
            "total_bytes": totalBytes,
            "error": errorText,
            "check_error": checkErrorText,
            "checked_at": checkedAtMs,
            "updated_from": updatedFrom,
            "updated_notes": updatedNotes,
            "updated_published_at": updatedPublishedAt,
        ]

        if let up = foundUpdate {
            payload["update"] = [
                "version": up.version,
                "tag": up.tag,
                "name": up.name,
                "published_at": up.publishedAt,
                "notes": up.notes,
                "html_url": up.htmlURL,
                "dmg_name": up.dmgName,
                "dmg_url": up.dmgURL,
                "dmg_size": up.dmgSize,
                "sha256": up.apiSha256,
            ]
        } else {
            payload["update"] = NSNull()
        }

        if let just = justUpdated {
            payload["just_updated"] = [
                "from": just.from,
                "to": just.to,
                "ok": just.ok,
                "notes": just.notes,
                "published_at": just.publishedAt,
            ]
        } else {
            payload["just_updated"] = NSNull()
        }

        return payload
    }

    private func emitStateChange() {
        let snap = snapshotDictionary()
        onStateChange?(snap)
    }

    func startPeriodicChecks() {
        if ProcessInfo.processInfo.environment["VIBE_HUB_DISABLE_AUTO_UPDATE_CHECK"] == "1" {
            return
        }
        scheduleNextCheck(after: initialCheckDelaySec)
    }

    private func scheduleNextCheck(after seconds: TimeInterval) {
        stateLock.lock()
        periodicTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: workQueue)
        timer.schedule(deadline: .now() + seconds)
        timer.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.performCheck(userInitiated: false) { succeeded in
                self.scheduleNextCheck(after: succeeded ? self.periodicCheckIntervalSec : self.retryCheckIntervalSec)
            }
        }
        periodicTimer = timer
        stateLock.unlock()
        timer.resume()
    }

    func checkNow() {
        performCheck(userInitiated: true, completion: nil)
    }

    private func performCheck(userInitiated: Bool, completion: ((Bool) -> Void)?) {
        stateLock.lock()
        if checkInFlight {
            stateLock.unlock()
            completion?(true)
            return
        }
        checkInFlight = true
        if userInitiated {
            checking = true
            latestConfirmed = false
            checkErrorText = ""
        }
        stateLock.unlock()

        if userInitiated {
            emitStateChange()
        }

        workQueue.async { [weak self] in
            guard let self = self else { return }
            let result = self.fetchLatestReleaseSync()
            let nowMs = Int64(Date().timeIntervalSince1970 * 1000)

            self.stateLock.lock()
            self.checkInFlight = false
            self.checking = false
            self.checkedAtMs = nowMs

            var succeeded = false
            switch result {
            case .success(let maybeRelease):
                succeeded = true
                self.checkErrorText = ""
                let busy = (self.status == "downloading" || self.status == "installing" || self.status == "ready" || self.status == "restarting")
                if !busy {
                    if let rel = maybeRelease {
                        if self.status == "failed", let prev = self.foundUpdate, prev.version == rel.version {
                            self.foundUpdate = rel
                        } else {
                            self.foundUpdate = rel
                            self.status = "available"
                            self.errorText = ""
                            self.percent = 0
                        }
                        self.latestConfirmed = false
                    } else {
                        self.foundUpdate = nil
                        self.status = "idle"
                        self.errorText = ""
                        self.percent = 0
                        if userInitiated {
                            self.latestConfirmed = true
                        }
                    }
                }
            case .failure(let err):
                NSLog("VibeSessionHub updater check failed: %@", err.localizedDescription)
                if userInitiated {
                    self.checkErrorText = err.localizedDescription
                    self.latestConfirmed = false
                }
            }
            self.stateLock.unlock()

            self.emitStateChange()
            completion?(succeeded)
        }
    }

    private func fetchLatestReleaseSync() -> Result<ReleaseUpdateInfo?, Error> {
        guard let url = URL(string: feedURL) else {
            return .failure(AppUpdaterError.invalidFeedURL)
        }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 15)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.setValue("VibeSessionHub/\(currentVersion)", forHTTPHeaderField: "User-Agent")

        let sem = DispatchSemaphore(value: 0)
        var outcome: Result<ReleaseUpdateInfo?, Error> = .failure(AppUpdaterError.invalidResponse)

        URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            defer { sem.signal() }
            guard let self = self else { return }
            if let err = err {
                outcome = .failure(err)
                return
            }
            guard let httpResp = resp as? HTTPURLResponse else {
                outcome = .failure(AppUpdaterError.invalidResponse)
                return
            }
            guard httpResp.statusCode == 200 else {
                outcome = .failure(AppUpdaterError.httpError(httpResp.statusCode))
                return
            }
            guard let data = data,
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                outcome = .failure(AppUpdaterError.invalidResponse)
                return
            }
            let parsed = parseGitHubReleasePayload(
                json,
                currentVersion: self.currentVersion,
                allowLoopback: self.allowLoopbackFeed
            )
            outcome = .success(parsed)
        }.resume()

        _ = sem.wait(timeout: .now() + 20)
        return outcome
    }

    func startUpdate() {
        guard case .supported(let targetAppURL) = capability else {
            return
        }

        stateLock.lock()
        guard let release = foundUpdate,
              status == "available" || status == "failed" else {
            stateLock.unlock()
            return
        }
        guard !release.dmgURL.isEmpty, let dmgURL = URL(string: release.dmgURL) else {
            status = "failed"
            errorText = AppUpdaterError.missingDmgAsset.localizedDescription
            stateLock.unlock()
            emitStateChange()
            return
        }
        status = "downloading"
        percent = 0
        downloadedBytes = 0
        totalBytes = release.dmgSize
        errorText = ""
        cancelling = false
        stateLock.unlock()
        emitStateChange()

        workQueue.async { [weak self] in
            self?.runDownloadAndStage(release: release, dmgURL: dmgURL, targetAppURL: targetAppURL)
        }
    }

    func cancelUpdate() {
        stateLock.lock()
        guard status == "downloading" else {
            stateLock.unlock()
            return
        }
        cancelling = true
        let downloader = activeDownloader
        let sidecar = activeSidecarTask
        stateLock.unlock()

        sidecar?.cancel()
        downloader?.cancel()
    }

    func restartUpdate() {
        stateLock.lock()
        guard status == "ready", let staged = stagedUpdate else {
            stateLock.unlock()
            return
        }
        status = "restarting"
        stateLock.unlock()
        emitStateChange()

        let relaunchURL = staged.workDir.appendingPathComponent("relaunch")
        let relaunchPayload = cliPortOverride.map(String.init) ?? ""
        try? Data(relaunchPayload.utf8).write(to: relaunchURL, options: .atomic)

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) { [weak self] in
            self?.onRequestTerminate?()
        }
    }

    func dismissJustUpdated() {
        stateLock.lock()
        justUpdated = nil
        stateLock.unlock()
        emitStateChange()
    }

    func openReleasePage() {
        stateLock.lock()
        let raw = foundUpdate?.htmlURL ?? ""
        stateLock.unlock()

        let fallback = "https://github.com/\(defaultUpdateGitHubRepo)/releases"
        let chosen = (!raw.isEmpty && isTrustedUpdateURL(raw, allowLoopback: allowLoopbackFeed)) ? raw : fallback
        if let url = URL(string: chosen) {
            DispatchQueue.main.async {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func openDownloadURL() {
        stateLock.lock()
        let rawDmg = foundUpdate?.dmgURL ?? ""
        let rawHtml = foundUpdate?.htmlURL ?? ""
        stateLock.unlock()

        let chosen: String
        if !rawDmg.isEmpty && isTrustedUpdateURL(rawDmg, allowLoopback: allowLoopbackFeed) {
            chosen = rawDmg
        } else if !rawHtml.isEmpty && isTrustedUpdateURL(rawHtml, allowLoopback: allowLoopbackFeed) {
            chosen = rawHtml
        } else {
            chosen = "https://github.com/\(defaultUpdateGitHubRepo)/releases"
        }
        if let url = URL(string: chosen) {
            DispatchQueue.main.async {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func handleAppWillTerminate() {
        stateLock.lock()
        periodicTimer?.cancel()
        periodicTimer = nil
        let currentStatus = status
        let downloader = activeDownloader
        let sidecar = activeSidecarTask
        let inProgressDir = activeWorkDir
        cancelling = true
        stateLock.unlock()

        // Cancel in-progress download and remove incomplete staging files.
        // Leave already-staged (ready / restarting) helper untouched so swap-on-exit completes cleanly.
        if currentStatus == "downloading" || currentStatus == "installing" {
            sidecar?.cancel()
            downloader?.cancel()
            if let dir = inProgressDir {
                try? FileManager.default.removeItem(at: dir)
            }
        }
    }

    private func fetchSidecarSha256Sync(urlString: String, expectedFilename: String) -> Result<String?, Error> {
        guard !urlString.isEmpty, let url = URL(string: urlString) else {
            return .success(nil)
        }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 15)
        req.setValue("VibeSessionHub/\(currentVersion)", forHTTPHeaderField: "User-Agent")

        let sem = DispatchSemaphore(value: 0)
        var outcome: Result<String?, Error> = .success(nil)

        let task = URLSession.shared.dataTask(with: req) { data, resp, err in
            defer { sem.signal() }
            if let err = err {
                let nsErr = err as NSError
                if nsErr.domain == NSURLErrorDomain && nsErr.code == NSURLErrorCancelled {
                    outcome = .failure(AppUpdaterError.cancelled)
                } else {
                    outcome = .failure(err)
                }
                return
            }
            guard let httpResp = resp as? HTTPURLResponse, httpResp.statusCode == 200,
                  let data = data, data.count <= 8192,
                  let text = String(data: data, encoding: .utf8) else {
                outcome = .success(nil)
                return
            }
            outcome = .success(parseSha256SidecarFile(text, expectedFilename: expectedFilename))
        }
        stateLock.lock()
        if cancelling {
            stateLock.unlock()
            return .failure(AppUpdaterError.cancelled)
        }
        activeSidecarTask = task
        stateLock.unlock()

        task.resume()
        _ = sem.wait(timeout: .now() + 20)

        stateLock.lock()
        activeSidecarTask = nil
        stateLock.unlock()
        return outcome
    }

    private func runDownloadAndStage(release: ReleaseUpdateInfo, dmgURL: URL, targetAppURL: URL) {
        let fm = FileManager.default
        let parentDir = targetAppURL.deletingLastPathComponent()
        let workDir = parentDir.appendingPathComponent(".\(targetAppURL.lastPathComponent).update-\(UUID().uuidString)")
        var keepWorkDir = false

        defer {
            stateLock.lock()
            activeDownloader = nil
            activeSidecarTask = nil
            activeWorkDir = nil
            stateLock.unlock()
            if !keepWorkDir {
                try? fm.removeItem(at: workDir)
            }
        }

        do {
            // 1. Resolve & reconcile expected SHA-256 checksum before downloading binary
            let sidecarDigest = try fetchSidecarSha256Sync(
                urlString: release.sha256URL,
                expectedFilename: release.dmgName
            ).get()

            let expectedSha256: String
            switch reconcileExpectedSha256(apiDigest: release.apiSha256, sidecarDigest: sidecarDigest) {
            case .ok(let hex):
                expectedSha256 = hex
            case .missing:
                throw AppUpdaterError.sha256Missing
            case .mismatch:
                throw AppUpdaterError.sha256Conflict
            }

            // 2. Prepare sibling staging folder on same volume as target .app
            try fm.createDirectory(at: workDir, withIntermediateDirectories: false)
            stateLock.lock()
            if cancelling {
                stateLock.unlock()
                throw AppUpdaterError.cancelled
            }
            activeWorkDir = workDir
            stateLock.unlock()

            // 3. Stream download DMG with incremental SHA-256 and throttled progress updates
            let dmgPath = workDir.appendingPathComponent("update.dmg")
            let downloader = StreamingDmgDownloader(
                destinationURL: dmgPath,
                expectedSize: release.dmgSize,
                allowLoopback: allowLoopbackFeed
            ) { [weak self] done, total, pct in
                guard let self = self else { return }
                self.stateLock.lock()
                if self.status == "downloading" {
                    self.downloadedBytes = done
                    if total > 0 { self.totalBytes = total }
                    self.percent = pct
                }
                self.stateLock.unlock()
                self.emitStateChange()
            }

            stateLock.lock()
            if cancelling {
                stateLock.unlock()
                throw AppUpdaterError.cancelled
            }
            activeDownloader = downloader
            stateLock.unlock()

            let dlResult = try downloader.downloadSync(
                from: dmgURL,
                userAgent: "VibeSessionHub/\(currentVersion)"
            ).get()

            // 4. Transition to verifying & installing phase
            stateLock.lock()
            status = "installing"
            percent = 100
            downloadedBytes = dlResult.bytes
            stateLock.unlock()
            emitStateChange()

            if release.dmgSize > 0 && dlResult.bytes != release.dmgSize {
                throw AppUpdaterError.sizeMismatch(expected: release.dmgSize, actual: dlResult.bytes)
            }
            if dlResult.sha256.lowercased() != expectedSha256.lowercased() {
                throw AppUpdaterError.sha256Mismatch
            }

            // 5. Mount DMG read-only in a dedicated temp mountpoint, copy .app via ditto, and always detach BEFORE staging
            let stagedAppURL = workDir.appendingPathComponent(targetAppURL.lastPathComponent)
            try extractAppBundleFromDmg(dmgPath: dmgPath, destinationAppURL: stagedAppURL)
            try? fm.removeItem(at: dmgPath)

            // 6. Validate bundle metadata, semver, code signature, and optional TeamIdentifier match
            try validateStagedBundle(
                stagedAppURL: stagedAppURL,
                targetAppURL: targetAppURL,
                expectedVersion: release.version
            )

            // 7. Spawn detached swap-on-exit helper process
            let helper = Process()
            helper.executableURL = URL(fileURLWithPath: "/bin/sh")
            helper.arguments = [
                "-c",
                buildSwapOnExitScript(),
                "vibe-hub-updater",
                String(getpid()),
                targetAppURL.path,
                stagedAppURL.path,
                workDir.path,
            ]
            helper.standardInput = FileHandle.nullDevice
            helper.standardOutput = FileHandle.nullDevice
            helper.standardError = FileHandle.nullDevice
            do {
                try helper.run()
            } catch {
                throw AppUpdaterError.helperSpawnFailed(error.localizedDescription)
            }

            keepWorkDir = true
            let newStaged = StagedBundleUpdate(
                workDir: workDir,
                stagedAppURL: stagedAppURL,
                targetAppURL: targetAppURL,
                helperProcess: helper
            )

            // Replace any previously staged update cleanly
            stateLock.lock()
            let previousStaged = stagedUpdate
            stagedUpdate = newStaged
            stateLock.unlock()

            if let prev = previousStaged {
                if prev.helperProcess.isRunning {
                    prev.helperProcess.terminate()
                    prev.helperProcess.waitUntilExit()
                }
                try? fm.removeItem(at: prev.workDir)
            }

            writeUpdateMarker(
                from: currentVersion,
                to: release.version,
                notes: release.notes,
                publishedAt: release.publishedAt
            )

            stateLock.lock()
            status = "ready"
            percent = 100
            errorText = ""
            stateLock.unlock()
            emitStateChange()

        } catch AppUpdaterError.cancelled {
            stateLock.lock()
            status = (foundUpdate != nil) ? "available" : "idle"
            percent = 0
            downloadedBytes = 0
            errorText = ""
            cancelling = false
            stateLock.unlock()
            emitStateChange()
        } catch {
            NSLog("VibeSessionHub updater failed: %@", error.localizedDescription)
            stateLock.lock()
            status = "failed"
            percent = 0
            downloadedBytes = 0
            errorText = error.localizedDescription
            cancelling = false
            stateLock.unlock()
            emitStateChange()
        }
    }

    private func extractAppBundleFromDmg(dmgPath: URL, destinationAppURL: URL) throws {
        let fm = FileManager.default
        let mountDir = fm.temporaryDirectory
            .appendingPathComponent("vibe-hub-dmg-\(UUID().uuidString)")
            .standardizedFileURL
            .resolvingSymlinksInPath()
        try fm.createDirectory(at: mountDir, withIntermediateDirectories: true)

        let attachRes = runSubprocess(
            "/usr/bin/hdiutil",
            arguments: [
                "attach", dmgPath.path,
                "-nobrowse",
                "-readonly",
                "-noautoopen",
                "-mountpoint", mountDir.path,
                "-plist",
            ]
        )
        guard attachRes.status == 0 else {
            try? fm.removeItem(at: mountDir)
            let detail = attachRes.stderr.isEmpty ? "exit \(attachRes.status)" : attachRes.stderr
            throw AppUpdaterError.mountFailed(detail)
        }

        // Parse mounted device & mount-point from hdiutil -plist output for guaranteed detachment
        var detachTargets: [String] = []
        if let plistData = attachRes.stdout.data(using: .utf8),
           let plist = (try? PropertyListSerialization.propertyList(from: plistData, options: [], format: nil)) as? [String: Any],
           let entities = plist["system-entities"] as? [[String: Any]] {
            for ent in entities {
                if let mp = ent["mount-point"] as? String, !mp.isEmpty {
                    detachTargets.append(mp)
                }
            }
            for ent in entities {
                if let dev = ent["dev-entry"] as? String, !dev.isEmpty {
                    detachTargets.append(dev)
                }
            }
        }
        detachTargets.append(mountDir.path)

        defer {
            var detached = false
            for target in detachTargets {
                let res = runSubprocess("/usr/bin/hdiutil", arguments: ["detach", target, "-quiet"])
                if res.status == 0 {
                    detached = true
                    break
                }
            }
            if !detached {
                for target in detachTargets {
                    let res = runSubprocess("/usr/bin/hdiutil", arguments: ["detach", target, "-force", "-quiet"])
                    if res.status == 0 {
                        break
                    }
                }
            }
            try? fm.removeItem(at: mountDir)
        }

        guard let mountedAppURL = locateAppBundleInMount(mountDir: mountDir) else {
            throw AppUpdaterError.appBundleNotFoundInDmg
        }

        let dittoRes = runSubprocess("/usr/bin/ditto", arguments: [mountedAppURL.path, destinationAppURL.path])
        guard dittoRes.status == 0 else {
            let detail = dittoRes.stderr.isEmpty ? "exit \(dittoRes.status)" : dittoRes.stderr
            throw AppUpdaterError.copyFailed(detail)
        }
    }

    private func locateAppBundleInMount(mountDir: URL) -> URL? {
        let fm = FileManager.default
        let expectedBundleId = Bundle.main.bundleIdentifier ?? "com.vibesessionhub.app"

        func isValidRealAppDirectory(_ url: URL) -> Bool {
            guard let vals = try? url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
                  vals.isDirectory == true,
                  vals.isSymbolicLink != true else {
                return false
            }
            let plistURL = url.appendingPathComponent("Contents/Info.plist")
            guard let data = try? Data(contentsOf: plistURL),
                  let dict = (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any],
                  let bid = dict["CFBundleIdentifier"] as? String,
                  bid == expectedBundleId else {
                return false
            }
            return true
        }

        let preferred = mountDir.appendingPathComponent("Vibe Session Hub.app")
        if isValidRealAppDirectory(preferred) {
            return preferred
        }

        guard let entries = try? fm.contentsOfDirectory(at: mountDir, includingPropertiesForKeys: nil) else {
            return nil
        }
        let matching = entries.filter {
            $0.pathExtension.lowercased() == "app" && isValidRealAppDirectory($0)
        }
        return matching.count == 1 ? matching[0] : nil
    }

    private func validateStagedBundle(
        stagedAppURL: URL,
        targetAppURL: URL,
        expectedVersion: String
    ) throws {
        let fm = FileManager.default
        let plistURL = stagedAppURL.appendingPathComponent("Contents/Info.plist")
        guard let data = try? Data(contentsOf: plistURL),
              let dict = (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any] else {
            throw AppUpdaterError.invalidBundle("缺少 Info.plist")
        }

        let expectedBundleId = Bundle.main.bundleIdentifier ?? "com.vibesessionhub.app"
        let bundleId = (dict["CFBundleIdentifier"] as? String) ?? ""
        guard bundleId == expectedBundleId else {
            throw AppUpdaterError.invalidBundle("Bundle ID 不匹配（\(bundleId)）")
        }

        let executable = (dict["CFBundleExecutable"] as? String) ?? ""
        guard !executable.isEmpty,
              fm.isExecutableFile(atPath: stagedAppURL.appendingPathComponent("Contents/MacOS/\(executable)").path) else {
            throw AppUpdaterError.invalidBundle("缺少主可执行文件")
        }

        let serverPy = stagedAppURL.appendingPathComponent("Contents/Resources/app/hub/server.py")
        guard fm.fileExists(atPath: serverPy.path) else {
            throw AppUpdaterError.invalidBundle("缺少内置后端服务")
        }

        let stagedVersion = ((dict["CFBundleShortVersionString"] as? String) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !stagedVersion.isEmpty,
              compareSemver(stagedVersion, currentVersion) > 0,
              compareSemver(stagedVersion, expectedVersion) == 0 else {
            throw AppUpdaterError.invalidBundle("版本号不匹配（期望 v\(expectedVersion)，实际 v\(stagedVersion)）")
        }

        let verifyRes = runSubprocess(
            "/usr/bin/codesign",
            arguments: ["--verify", "--deep", "--strict", stagedAppURL.path]
        )
        guard verifyRes.status == 0 else {
            let detail = verifyRes.stderr.isEmpty ? "exit \(verifyRes.status)" : verifyRes.stderr
            throw AppUpdaterError.codesignVerifyFailed(detail)
        }

        // If the currently installed app has a Developer ID TeamIdentifier, enforce identical TeamIdentifier.
        let currentSign = runSubprocess("/usr/bin/codesign", arguments: ["-dv", targetAppURL.path])
        if let currentTeam = parseCodesignTeamIdentifier(currentSign.stderr) {
            let stagedSign = runSubprocess("/usr/bin/codesign", arguments: ["-dv", stagedAppURL.path])
            let stagedTeam = parseCodesignTeamIdentifier(stagedSign.stderr)
            if stagedTeam != currentTeam {
                throw AppUpdaterError.teamIdentifierMismatch
            }
        }
    }

    private func runSubprocess(_ executable: String, arguments: [String]) -> (status: Int32, stdout: String, stderr: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = arguments
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        do {
            try proc.run()
        } catch {
            return (-1, "", error.localizedDescription)
        }

        var outData = Data()
        var errData = Data()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global(qos: .utility).async {
            outData = outPipe.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        group.enter()
        DispatchQueue.global(qos: .utility).async {
            errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }

        proc.waitUntilExit()
        group.wait()

        let outStr = (String(data: outData, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let errStr = (String(data: errData, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return (proc.terminationStatus, outStr, errStr)
    }
}
