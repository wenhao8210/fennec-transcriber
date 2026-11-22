import AppKit
import Darwin
import WebKit
import UniformTypeIdentifiers

// Wennec 转写 —— macOS 原生外壳
// 职责：托管 uvicorn 后端进程（随 App 启动/退出）、提供原生窗口与菜单栏常驻入口。

let PORT: Int = {
    if let raw = ProcessInfo.processInfo.environment["FENNEC_PORT"], let value = Int(raw),
       (1024...65535).contains(value) { return value }
    return 8778
}()
let LOG_PATH = ProcessInfo.processInfo.environment["FENNEC_LOG_PATH"]
    ?? NSHomeDirectory() + "/Library/Logs/Wennec/server.log"
let FENNEC_DATA_DIR = NSHomeDirectory() + "/Library/Application Support/com.kkapp.fennec/datas"

// MARK: - 路径

func projectRoot() -> String {
    if let env = ProcessInfo.processInfo.environment["FENNEC_HOME"], !env.isEmpty { return env }
    let root = Bundle.main.bundleURL.deletingLastPathComponent().path
    if FileManager.default.fileExists(atPath: root + "/backend/main.py") { return root }
    return root
}

/// GUI 进程 PATH 极简，补上 Homebrew 前缀，保证 ffmpeg/ffprobe 能被后端调到。
func shellPath() -> String {
    let base = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
    return "/opt/homebrew/bin:/usr/local/bin:" + base
}

// MARK: - 后端进程管理

final class ServerManager {
    let root: String
    private var process: Process?
    private var logHandle: FileHandle?
    private var wantsServer = false
    private var lastStartAttempt = Date.distantPast

    init(root: String) { self.root = root }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(PORT)/")! }

    /// 服务是否活着（直接 TCP 探活 127.0.0.1:8778，比 HTTP 更省、也不会被路由影响）
    func isHealthy() -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(PORT).bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let ret = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return ret == 0
    }

    /// 轻量健康检查，不扫描录音目录或读取正文。未知/忙碌状态绝不据此杀进程。
    private func healthSnapshot() -> [String: Any]? {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/health"))
        req.httpMethod = "GET"
        req.timeoutInterval = 3
        let sem = DispatchSemaphore(value: 0)
        let lock = NSLock()
        var result: [String: Any]?
        let task = URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200, let data,
               let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
               json["service"] as? String == "fennec-transcriber" {
                lock.lock(); result = json; lock.unlock()
            }
            sem.signal()
        }
        task.resume()
        if sem.wait(timeout: .now() + 4) == .timedOut { task.cancel() }
        lock.lock(); defer { lock.unlock() }
        return result
    }

    func isServing() -> Bool { healthSnapshot() != nil }

    private func canStopServer() -> Bool {
        guard let health = healthSnapshot(), let active = health["active_transcriptions"] as? Int else { return false }
        return active == 0 && (health["active_recording_jobs"] as? Int ?? 0) == 0
    }

    @discardableResult
    func ensureRunning(timeout: TimeInterval = 120) -> Bool {
        if isServing() { return true }
        start()
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            Thread.sleep(forTimeInterval: 0.5)
            if isServing() { return true }
        }
        return false
    }

    func start() {
        guard !isHealthy() else { return }
        lastStartAttempt = Date()
        wantsServer = true

        let fm = FileManager.default
        try? fm.createDirectory(atPath: (LOG_PATH as NSString).deletingLastPathComponent,
                                withIntermediateDirectories: true)
        if !fm.fileExists(atPath: LOG_PATH) { fm.createFile(atPath: LOG_PATH, contents: nil) }
        let handle = FileHandle(forWritingAtPath: LOG_PATH)
        handle?.seekToEndOfFile()
        logHandle = handle

        let p = Process()
        p.executableURL = URL(fileURLWithPath: root + "/venv/bin/uvicorn")
        p.arguments = ["main:app", "--port", String(PORT), "--host", "127.0.0.1"]
        p.currentDirectoryURL = URL(fileURLWithPath: root + "/backend")
        if let handle { p.standardOutput = handle; p.standardError = handle }
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = shellPath()
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        p.terminationHandler = { [weak self] _ in
            // 进程意外退出（崩溃/被杀）：等 2s 再自动拉起，保证 App 开着服务就在
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self?.recoverIfNeeded() }
        }
        do {
            try p.run()
            process = p
        } catch {
            NSLog("[Wennec] 启动后端失败: \(error)")
        }
    }

    private func recoverIfNeeded() {
        guard wantsServer, !isHealthy() else { return }
        guard Date().timeIntervalSince(lastStartAttempt) > 10 else { return }
        start()
    }

    /// 用户主动重启时也保留转写任务，不因 HTTP 响应慢强杀后端。
    @discardableResult
    func restart() -> Bool {
        if isHealthy() && !canStopServer() { return false }
        killPortOwner()
        process = nil
        Thread.sleep(forTimeInterval: 1)
        lastStartAttempt = .distantPast
        start()
        return true
    }

    /// 只管理此项目的 uvicorn，不按通用 main:app 名称匹配其他服务。
    private func portOwnerPIDs() -> [Int32] {
        let lsof = Process()
        lsof.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        lsof.arguments = ["-nP", "-iTCP:\(PORT)", "-sTCP:LISTEN", "-t"]
        let pipe = Pipe()
        lsof.standardOutput = pipe
        try? lsof.run()
        lsof.waitUntilExit()
        let text = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""

        var pids: [Int32] = []
        for line in text.split(separator: "\n") {
            guard let pid = Int32(line.trimmingCharacters(in: .whitespaces)), pid > 1 else { continue }
            if isWennecServer(pid) { pids.append(pid) }
        }
        return pids
    }

    private func isWennecServer(_ pid: Int32) -> Bool {
        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-o", "command=", "-p", String(pid)]
        let pipe = Pipe()
        ps.standardOutput = pipe
        try? ps.run()
        ps.waitUntilExit()
        let cmd = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        guard cmd.contains("main:app") else { return false }
        if cmd.contains(root + "/venv/bin/uvicorn") { return true }
        let cwd = Process()
        cwd.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        cwd.arguments = ["-a", "-p", String(pid), "-d", "cwd", "-Fn"]
        let cwdPipe = Pipe()
        cwd.standardOutput = cwdPipe
        cwd.standardError = FileHandle.nullDevice
        do { try cwd.run() } catch { return false }
        cwd.waitUntilExit()
        let output = String(data: cwdPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return output.split(separator: "\n").contains(Substring("n" + root + "/backend"))
    }

    private func killPortOwner() {
        for pid in portOwnerPIDs() { kill(pid, SIGTERM) }
        let deadline = Date().addingTimeInterval(4)
        while Date() < deadline {
            if portOwnerPIDs().isEmpty { break }
            Thread.sleep(forTimeInterval: 0.2)
        }
        for pid in portOwnerPIDs() { kill(pid, SIGKILL) }
    }

    /// 退出只停止自己启动且空闲的服务；转写中或外部启动的服务继续运行。
    func stop() {
        wantsServer = false
        if let p = process, p.isRunning {
            p.terminationHandler = nil
            if canStopServer() { p.terminate() }
        }
        process = nil
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSMenuItemValidation, NSMenuDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var statusMenuItem: NSMenuItem!
    private var server: ServerManager!
    private var rootPath = ""
    private var healthTimer: Timer?
    private var recorder: RecordingController!
    private var recorderMenuItem: NSMenuItem!
    private var recordingsDirectory: URL!
    private var terminating = false
    private var recorderReadyToQuit = false
    private var listeningMenuItems: [NSMenuItem] = []
    private var segmentMenuItems: [NSMenuItem] = []
    private var transcribeMenuItems: [NSMenuItem] = []
    private var canTranscribeCurrent = false
    private var frontendMenuRefreshInFlight = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        rootPath = projectRoot()
        server = ServerManager(root: rootPath)
        let recordingPath = ProcessInfo.processInfo.environment["FENNEC_RECORDINGS_DIR"]
            ?? NSHomeDirectory() + "/Library/Application Support/local.fennec-transcriber/Recordings"
        recordingsDirectory = URL(fileURLWithPath: recordingPath, isDirectory: true)
        let recorderDefaults = ProcessInfo.processInfo.environment["FENNEC_DEFAULTS_SUITE"]
            .flatMap { UserDefaults(suiteName: $0) } ?? .standard
        recorder = RecordingController(recordingsDirectory: recordingsDirectory, baseURL: server.baseURL, defaults: recorderDefaults)

        let backgroundValidation = PORT != 8778 && ProcessInfo.processInfo.environment["FENNEC_TEST_NO_ACTIVATE"] == "1"
        NSApp.setActivationPolicy(backgroundValidation ? .accessory : .regular)
        buildMainMenu()
        buildStatusItem()
        buildWindow()
        if backgroundValidation { statusItem.isVisible = false }
        recorder.onStatus = { [weak self] snapshot in
            DispatchQueue.main.async { self?.publishRecorderStatus(snapshot) }
        }
        publishRecorderStatus(recorder.snapshot())

        if !backgroundValidation {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }

        boot()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            self?.healthCheck()
        }
    }

    // 关掉窗口不退出，Dock / 菜单栏随时唤回
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        healthTimer?.invalidate()
        server.stop()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if recorderReadyToQuit || recorder == nil { return .terminateNow }
        if terminating { return .terminateLater }
        terminating = true
        healthTimer?.invalidate()
        recorder.prepareForTermination { [weak self] in
            self?.recorderReadyToQuit = true
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// 被信号（kill / Ctrl-C）结束时调用
    func stopServerAndExit() {
        guard !terminating else { return }
        NSApp.terminate(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    // MARK: 启动 / 健康检查

    private var booting = false
    private var showingPlaceholder = true

    /// 启动/重启流程：拉起服务 → 加载页面。失败会自动重试一次（先杀掉旧进程再起）
    private func boot(retry: Int = 0) {
        guard !booting else { return }
        booting = true
        setStatus("正在启动服务…")
        loadHTML(loadingPage())

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var ok = self.server.ensureRunning()
            if !ok && retry == 0 && !self.server.isHealthy() {
                self.server.start()
                ok = self.server.ensureRunning()
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.booting = false
                if ok {
                    self.webView.load(URLRequest(url: self.server.baseURL))
                    self.showingPlaceholder = false
                    self.setStatus("服务运行中 · 端口 \(PORT)")
                } else {
                    self.loadHTML(self.errorPage())
                    self.setStatus("服务启动失败")
                }
            }
        }
    }

    private func healthCheck() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let ok = self.server.isServing()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if ok {
                    self.setStatus("服务运行中 · 端口 \(PORT)")
                    return
                }
                guard !self.booting else { return }
                if self.server.isHealthy() {
                    self.setStatus("服务暂忙，等待响应；录音可继续")
                    return
                }
                self.setStatus("服务已停止，正在重启…")
                self.booting = true
                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                    guard let self else { return }
                    self.server.start()
                    let recovered = self.server.ensureRunning()
                    DispatchQueue.main.async { [weak self] in
                        guard let self else { return }
                        self.booting = false
                        if recovered {
                            self.setStatus("服务运行中 · 端口 \(PORT)")
                            if self.showingPlaceholder {
                                self.webView.load(URLRequest(url: self.server.baseURL))
                                self.showingPlaceholder = false
                            } else {
                                self.webView.reload()
                            }
                        } else {
                            self.setStatus("服务启动失败")
                        }
                    }
                }
            }
        }
    }

    private func setStatus(_ text: String) {
        statusMenuItem.title = "状态：" + text
        statusItem.button?.toolTip = "Wennec 转写 · " + text
    }

    // MARK: 窗口

    private func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 880)
        let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable]
        let win = NSWindow(contentRect: frame, styleMask: style, backing: .buffered, defer: false)
        win.title = PORT == 8778 ? "Wennec 转写" : "Wennec 转写 · 隔离验收（不含历史录音）"
        win.minSize = NSSize(width: 900, height: 600)
        win.center()
        win.delegate = self
        win.isReleasedWhenClosed = false
        win.setFrameAutosaveName("WennecMainWindow")

        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []
        // 前端复制走原生剪贴板：WKWebView 里 navigator.clipboard / execCommand
        // 在异步取数后已失去用户手势，必然失败
        config.userContentController.add(self, name: "hubCopy")
        config.userContentController.add(self, name: "hubDownload")
        config.userContentController.add(self, name: "fennecRecorder")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        let wv = WKWebView(frame: .zero, configuration: config)
        wv.navigationDelegate = self
        wv.allowsBackForwardNavigationGestures = true  // 兜底：万一页面被导航走，可用双指滑动/手势返回
        wv.autoresizingMask = [.width, .height]
        win.contentView = wv

        window = win
        webView = wv
    }

    // MARK: 转写

    /// ⌘T：用当前选择的方式转写。
    /// 走前端按钮而不是直接调接口，这样按钮的禁用态（未选中 / 转写中）一并复用。
    @objc func transcribeCurrent() {
        showMainWindow()
        guard frontendReady else { NSSound.beep(); return }
        let js = """
        (() => {
          if (window.fennecTranscribeCurrent) return window.fennecTranscribeCurrent();
          if (window.fennecNavigate) window.fennecNavigate('library');
          const b = document.getElementById('btnTranscribe');
          if (!b) return 'no-button';
          if (b.disabled) return 'disabled';
          b.click();
          return 'ok';
        })()
        """
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak self] in
            guard let self = self else { return }
            self.webView.evaluateJavaScript(js) { result, error in
                if let error = error {
                    print("转写失败：\(error)")
                    NSSound.beep()
                    return
                }
                if let state = result as? String, state != "ok" {
                    // 没有选中录音，或正在转写
                    NSSound.beep()
                }
            }
        }
    }

    // MARK: 菜单

    private func isLocalAppURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == PORT
            && url.user == nil && url.password == nil
    }

    private func publishRecorderStatus(_ snapshot: [String: Any]) {
        let state = snapshot["state"] as? String ?? "idle"
        let labels = ["idle": "待命", "requesting": "等待麦克风授权", "recording": "录音中",
                      "listening": "监听中，等待声音", "paused": "已暂停", "interrupted": "采集中断",
                      "error": "采集异常", "stopping": "正在保存"]
        let elapsed = max(0, (snapshot["elapsed"] as? NSNumber)?.intValue ?? 0)
        recorderMenuItem?.title = "\(labels[state] ?? state) · \(String(format: "%02d:%02d", elapsed / 60, elapsed % 60))"
        let active = snapshot["recording"] as? Bool ?? (state == "recording")
        let listening = hasListeningIntent(snapshot)
        updateRecorderMenuTitles(snapshot)
        statusItem?.button?.image = NSImage(systemSymbolName: active ? "record.circle.fill" : (listening ? "ear.badge.waveform" : "waveform"),
                                           accessibilityDescription: active ? "正在录音" : (listening ? "正在监听" : "Wennec"))
        statusItem?.button?.contentTintColor = active ? .systemRed : (listening ? .systemOrange : nil)
        guard window?.isVisible == true, isLocalAppURL(webView?.url),
              JSONSerialization.isValidJSONObject(snapshot),
              let data = try? JSONSerialization.data(withJSONObject: snapshot),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.fennecRecorderUpdate && window.fennecRecorderUpdate(\(json))",
                                   completionHandler: nil)
        refreshFrontendMenuState()
    }

    private func hasListeningIntent(_ snapshot: [String: Any]) -> Bool {
        if snapshot["listening"] as? Bool == true { return true }
        let state = snapshot["state"] as? String ?? "idle"
        return snapshot["mode"] as? String == "listening" && !["idle", "error"].contains(state)
    }

    @objc private func toggleListening() {
        recorder.handleCommand(hasListeningIntent(recorder.snapshot()) ? "stopListening" : "listen")
    }

    @objc private func toggleSegment() {
        recorder.handleCommand(recorder.snapshot()["recording"] as? Bool == true ? "endSegment" : "record")
    }

    @objc private func showRecordingOptions() {
        performFrontendAction("window.fennecShowRecordingOptions && window.fennecShowRecordingOptions()")
    }

    @objc private func showListeningView() {
        performFrontendAction("window.fennecNavigate && window.fennecNavigate('listening')")
    }

    @objc private func showLibraryView() {
        performFrontendAction("window.fennecNavigate && window.fennecNavigate('library')")
    }

    @objc private func showSettings() {
        performFrontendAction("window.fennecShowSettings && window.fennecShowSettings()")
    }

    @objc private func showSearch() {
        performFrontendAction("window.fennecShowSearch && window.fennecShowSearch()")
    }

    private var frontendReady: Bool {
        !booting && !showingPlaceholder && webView != nil && !webView.isLoading && isLocalAppURL(webView.url)
    }

    private func performFrontendAction(_ expression: String) {
        showMainWindow()
        guard frontendReady else { NSSound.beep(); return }
        webView.evaluateJavaScript(expression, completionHandler: nil)
    }

    private func refreshFrontendMenuState() {
        guard frontendReady else {
            canTranscribeCurrent = false
            transcribeMenuItems.forEach { $0.isEnabled = false }
            return
        }
        guard !frontendMenuRefreshInFlight else { return }
        frontendMenuRefreshInFlight = true
        webView.evaluateJavaScript("Boolean(document.getElementById('btnTranscribe') && !document.getElementById('btnTranscribe').disabled)") { [weak self] value, _ in
            guard let self = self else { return }
            self.frontendMenuRefreshInFlight = false
            self.canTranscribeCurrent = self.frontendReady && (value as? Bool ?? false)
            self.transcribeMenuItems.forEach { $0.isEnabled = self.canTranscribeCurrent && !self.terminating }
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        if recorder != nil { updateRecorderMenuTitles(recorder.snapshot()) }
        refreshFrontendMenuState()
    }

    private func updateRecorderMenuTitles(_ snapshot: [String: Any]) {
        let listening = hasListeningIntent(snapshot)
        let active = snapshot["recording"] as? Bool ?? false
        listeningMenuItems.forEach { $0.title = listening ? "停止监听并保存" : "开始监听" }
        segmentMenuItems.forEach { $0.title = active ? "结束这一段，继续监听" : "立即录音" }
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        guard let action = menuItem.action else { return false }
        guard !terminating else { return false }
        let snapshot = recorder?.snapshot() ?? [:]
        let state = snapshot["state"] as? String ?? "idle"
        let capturing = ["recording", "listening"].contains(state)
        let transition = ["requesting", "stopping"].contains(state)
        switch action {
        case #selector(toggleListening):
            return state != "stopping" && (state != "requesting" || hasListeningIntent(snapshot))
        case #selector(toggleSegment): return !transition
        case #selector(startRecording): return ["idle", "error"].contains(state)
        case #selector(pauseRecording): return capturing
        case #selector(resumeRecording): return ["paused", "interrupted"].contains(state)
        case #selector(splitRecording): return capturing && snapshot["recording"] as? Bool == true
        case #selector(stopRecording): return !["idle", "error", "stopping"].contains(state)
        case #selector(transcribeCurrent): return frontendReady && canTranscribeCurrent
        case #selector(showListeningView), #selector(showLibraryView), #selector(showRecordingOptions),
             #selector(showSettings), #selector(showSearch): return frontendReady
        case #selector(restartServer): return !booting
        default: return true
        }
    }

    @objc private func startRecording() { recorder.start() }
    @objc private func pauseRecording() { recorder.pause() }
    @objc private func resumeRecording() { recorder.resume() }
    @objc private func stopRecording() { recorder.stop() }
    @objc private func splitRecording() { recorder.split() }
    @objc private func openOwnRecordingsDir() { NSWorkspace.shared.open(recordingsDirectory) }

    private func menuItem(_ title: String, action: Selector?, key: String = "", target: AnyObject? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = action == nil ? nil : (target ?? self)
        return item
    }

    private func recordingMenuItems(shortcuts: Bool, advanced: Bool) -> [NSMenuItem] {
        let listen = menuItem("开始监听", action: #selector(toggleListening), key: shortcuts ? "l" : "")
        listeningMenuItems.append(listen)
        let segment = menuItem("立即录音", action: #selector(toggleSegment), key: shortcuts ? "r" : "")
        segment.keyEquivalentModifierMask = [.command, .shift]
        segmentMenuItems.append(segment)
        var items: [NSMenuItem] = [
            listen,
            segment,
            menuItem("保存当前段并继续", action: #selector(splitRecording)),
            menuItem("停止所有采集并保存", action: #selector(stopRecording)),
        ]
        if advanced {
            items += [
                .separator(),
                menuItem("暂停采集", action: #selector(pauseRecording)),
                menuItem("继续采集", action: #selector(resumeRecording)),
                menuItem("手动连续录音", action: #selector(startRecording)),
            ]
        }
        return items
    }

    private func currentTranscriptionMenuItem() -> NSMenuItem {
        let item = menuItem("转写当前录音", action: #selector(transcribeCurrent), key: "t")
        item.isEnabled = false
        transcribeMenuItems.append(item)
        return item
    }

    private func buildMainMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(menuItem("关于 Wennec", action: #selector(showAbout)))
        appMenu.addItem(.separator())
        appMenu.addItem(menuItem("设置…", action: #selector(showSettings), key: ","))
        appMenu.addItem(.separator())
        let hide = menuItem("隐藏 Wennec", action: #selector(NSApplication.hide(_:)), key: "h", target: NSApp)
        let hideOthers = menuItem("隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), key: "h", target: NSApp)
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        let showAll = menuItem("显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), target: NSApp)
        appMenu.addItem(hide)
        appMenu.addItem(hideOthers)
        appMenu.addItem(showAll)
        appMenu.addItem(.separator())
        appMenu.addItem(menuItem("退出 Wennec", action: #selector(NSApplication.terminate(_:)), key: "q", target: NSApp))
        let appMenuItem = NSMenuItem()
        appMenuItem.submenu = appMenu
        main.addItem(appMenuItem)

        let edit = NSMenu(title: "编辑")
        let editPairs: [(String, Selector, String)] = [
            ("撤销", NSSelectorFromString("undo:"), "z"),
            ("重做", NSSelectorFromString("redo:"), "Z"),
            ("剪切", NSSelectorFromString("cut:"), "x"),
            ("拷贝", NSSelectorFromString("copy:"), "c"),
            ("粘贴", NSSelectorFromString("paste:"), "v"),
            ("全选", NSSelectorFromString("selectAll:"), "a"),
        ]
        for (title, action, key) in editPairs {
            edit.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key))
        }
        let editItem = NSMenuItem()
        editItem.submenu = edit
        main.addItem(editItem)

        let view = NSMenu(title: "视图")
        view.delegate = self
        view.addItem(menuItem("监听", action: #selector(showListeningView), key: "1"))
        view.addItem(menuItem("录音库", action: #selector(showLibraryView), key: "2"))
        view.addItem(.separator())
        view.addItem(menuItem("显示主窗口", action: #selector(showMainWindow), key: "o"))
        view.addItem(menuItem("重新加载页面", action: #selector(reloadPage), key: "r"))
        view.addItem(menuItem("在浏览器中打开", action: #selector(openInBrowser), key: "b"))
        view.addItem(.separator())
        let tools = NSMenu(title: "工具与诊断")
        tools.addItem(menuItem("重启服务", action: #selector(restartServer)))
        tools.addItem(.separator())
        tools.addItem(menuItem("打开转写数据目录", action: #selector(openDataDir)))
        tools.addItem(menuItem("打开旧 Wennec 录音目录", action: #selector(openRecordingsDir)))
        tools.addItem(menuItem("在 Finder 中显示日志", action: #selector(revealLog)))
        let toolsItem = NSMenuItem(title: "工具与诊断", action: nil, keyEquivalent: "")
        toolsItem.submenu = tools
        view.addItem(toolsItem)
        let viewItem = NSMenuItem(title: "视图", action: nil, keyEquivalent: "")
        viewItem.submenu = view
        main.addItem(viewItem)

        let listening = NSMenu(title: "监听")
        listening.delegate = self
        recordingMenuItems(shortcuts: true, advanced: true).forEach { listening.addItem($0) }
        listening.addItem(.separator())
        listening.addItem(menuItem("监听选项…", action: #selector(showRecordingOptions)))
        listening.addItem(menuItem("打开我的录音目录", action: #selector(openOwnRecordingsDir)))
        let listeningItem = NSMenuItem(title: "监听", action: nil, keyEquivalent: "")
        listeningItem.submenu = listening
        main.addItem(listeningItem)

        let transcription = NSMenu(title: "转写")
        transcription.delegate = self
        transcription.addItem(currentTranscriptionMenuItem())
        let search = menuItem("搜索全部转写…", action: #selector(showSearch), key: "f")
        search.keyEquivalentModifierMask = [.command, .shift]
        transcription.addItem(search)
        transcription.addItem(.separator())
        transcription.addItem(menuItem("转写设置…", action: #selector(showSettings)))
        let transcriptionItem = NSMenuItem(title: "转写", action: nil, keyEquivalent: "")
        transcriptionItem.submenu = transcription
        main.addItem(transcriptionItem)

        let win = NSMenu(title: "窗口")
        win.addItem(NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        win.addItem(NSMenuItem(title: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        win.addItem(NSMenuItem(title: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: ""))
        win.addItem(.separator())
        win.addItem(NSMenuItem(title: "前置全部窗口", action: #selector(NSApplication.arrangeInFront(_:)), keyEquivalent: ""))
        let winItem = NSMenuItem()
        winItem.submenu = win
        main.addItem(winItem)

        NSApp.mainMenu = main
        NSApp.servicesMenu = nil
        NSApp.windowsMenu = win
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Wennec")
            button.toolTip = "Wennec 转写"
        }
        let menu = NSMenu()
        menu.delegate = self
        menu.addItem(menuItem("打开监听", action: #selector(showListeningView)))
        menu.addItem(menuItem("打开录音库", action: #selector(showLibraryView)))
        menu.addItem(.separator())
        statusMenuItem = menuItem("状态：启动中…", action: nil)
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        recorderMenuItem = menuItem("录音：待命", action: nil)
        recorderMenuItem.isEnabled = false
        menu.addItem(recorderMenuItem)
        menu.addItem(.separator())
        recordingMenuItems(shortcuts: false, advanced: false).forEach { menu.addItem($0) }
        menu.addItem(menuItem("监听选项…", action: #selector(showRecordingOptions)))
        menu.addItem(.separator())
        menu.addItem(menuItem("设置…", action: #selector(showSettings)))
        menu.addItem(menuItem("退出 Wennec", action: #selector(NSApplication.terminate(_:)), key: "q", target: NSApp))
        statusItem.menu = menu
    }

    // MARK: 动作

    @objc private func showMainWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if recorder != nil { publishRecorderStatus(recorder.snapshot()) }
    }

    @objc private func reloadPage() {
        webView.load(URLRequest(url: server.baseURL))
        showingPlaceholder = false
    }

    @objc private func openInBrowser() {
        NSWorkspace.shared.open(server.baseURL)
    }

    @objc private func restartServer() {
        guard !booting, !terminating else { return }
        booting = true
        setStatus("正在检查服务…")
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let restarted = self.server.restart()
            DispatchQueue.main.async {
                self.booting = false
                if restarted { self.boot() }
                else { self.setStatus("转写进行中或服务暂忙，未重启") }
            }
        }
    }

    @objc private func openRecordingsDir() {
        let sessions = FENNEC_DATA_DIR + "/Sessions"
        let url = URL(fileURLWithPath: FileManager.default.fileExists(atPath: sessions) ? sessions : FENNEC_DATA_DIR,
                      isDirectory: true)
        NSWorkspace.shared.open(url)
    }

    @objc private func openDataDir() {
        NSWorkspace.shared.open(URL(fileURLWithPath: rootPath + "/data", isDirectory: true))
    }

    @objc private func revealLog() {
        let fm = FileManager.default
        if !fm.fileExists(atPath: LOG_PATH) {
            try? fm.createDirectory(atPath: (LOG_PATH as NSString).deletingLastPathComponent,
                                    withIntermediateDirectories: true)
            fm.createFile(atPath: LOG_PATH, contents: nil)
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: LOG_PATH)])
    }

    @objc private func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let credits = NSMutableAttributedString(string: "持续监听 · 自动分段 · 时间轴选区\n原生麦克风采集 + 本地 Whisper / API 转写\n数据目录：\(rootPath)/data")
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Wennec 转写",
            .applicationVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.1.0",
            .credits: credits,
        ])
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.request.url?.scheme == "fennec" {
            decisionHandler(.cancel)
            boot(retry: 0)
            return
        }
        if let url = navigationAction.request.url, !isLocalAppURL(url), url.scheme != "about" {
            decisionHandler(.cancel)
            if ["https", "http", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            return
        }
        decisionHandler(.allow)
    }

    // MARK: 外部跳转（fennec://<sessionId>?t=秒）

    private var pendingJump: (sid: String, t: String)?

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls where ["wennec", "fennec"].contains(url.scheme?.lowercased() ?? "") {
            let sid = (url.host ?? "").filter { $0.isHexDigit }
            let t = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "t" })?.value ?? "0"
            jumpTo(sessionId: sid, seconds: t)
        }
    }

    private func jumpTo(sessionId: String, seconds: String) {
        guard !sessionId.isEmpty else { return }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        guard webView != nil, webView.url != nil else {
            pendingJump = (sessionId, seconds)
            return
        }
        applyJump(sessionId: sessionId, seconds: seconds)
    }

    private func applyJump(sessionId: String, seconds: String) {
        let sec = Double(seconds) ?? 0
        let js = "(window.fennecOpen && window.fennecOpen('\(sessionId)', \(sec)), 'ok')"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        publishRecorderStatus(recorder.snapshot())
        if PORT != 8778 && isLocalAppURL(webView.url) {
            webView.evaluateJavaScript("""
            (() => {
              if (document.getElementById('native-test-banner')) return;
              const notice = document.createElement('div');
              notice.id = 'native-test-banner';
              notice.textContent = '隔离验收 · 不含历史录音';
              notice.style.cssText = 'position:fixed;right:14px;bottom:12px;z-index:99999;padding:7px 12px;border-radius:8px;background:#5a3917;color:#ffe0a9;font:12px -apple-system;pointer-events:none';
              document.body.appendChild(notice);
            })()
            """, completionHandler: nil)
        }
        if let p = pendingJump {
            pendingJump = nil
            applyJump(sessionId: p.sid, seconds: p.t)
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // -999 / NSURLErrorCancelled：上一个导航被新导航取消（切换加载目标时的正常现象），不是故障
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        loadHTML(errorPage(detail: ns.localizedDescription))
        setStatus("页面加载失败")
    }

    // MARK: 剪贴板

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,
              origin.protocol == "http", origin.host == "127.0.0.1", origin.port == PORT else { return }
        if message.name == "fennecRecorder" {
            guard !terminating, let body = message.body as? [String: Any],
                  let command = body["command"] as? String else { return }
            recorder.handleCommand(command, options: body["options"] as? [String: Any] ?? [:])
            return
        }
        if message.name == "hubDownload" {
            exportSelection(message)
            return
        }
        guard message.name == "hubCopy", let text = message.body as? String else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }

    // MARK: 文件导出

    private var downloadingSelection = false

    private func reportDownload(_ result: [String: Any]) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.downloadingSelection = false
            guard let data = try? JSONSerialization.data(withJSONObject: result),
                  let json = String(data: data, encoding: .utf8) else { return }
            self.webView.evaluateJavaScript(
                "window.fennecDownloadResult && window.fennecDownloadResult(\(json))",
                completionHandler: nil
            )
        }
    }

    private func exportSelection(_ message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,
              origin.protocol == "http", origin.host == "127.0.0.1", origin.port == PORT,
              let body = message.body as? [String: Any],
              let path = body["url"] as? String,
              let url = URL(string: path, relativeTo: server.baseURL)?.absoluteURL,
              url.scheme == "http", url.host == "127.0.0.1", url.port == PORT,
              ["/api/export-audio", "/api/export-text"].contains(url.path),
              url.user == nil, url.password == nil else {
            reportDownload(["ok": false, "error": "导出地址无效"])
            return
        }
        guard !downloadingSelection else { return }
        downloadingSelection = true

        let isText = url.path == "/api/export-text"
        let panel = NSSavePanel()
        panel.title = isText ? "导出 TXT" : "导出录音"
        panel.prompt = "导出"
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [isText ? .plainText : .zip]
        panel.nameFieldStringValue = isText ? "Wennec转写.txt" : "Wennec录音.zip"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            guard response == .OK, let destination = panel.url else {
                self.reportDownload(["ok": false, "cancelled": true])
                return
            }
            let request = URLRequest(url: url, timeoutInterval: 600)
            URLSession.shared.downloadTask(with: request) { [weak self] temporary, response, error in
                guard let self else { return }
                if let error {
                    self.reportDownload(["ok": false, "error": "导出失败：" + error.localizedDescription])
                    return
                }
                guard let http = response as? HTTPURLResponse, let temporary else {
                    self.reportDownload(["ok": false, "error": "导出失败"])
                    return
                }
                guard (200..<300).contains(http.statusCode),
                      http.url?.host == "127.0.0.1", http.url?.port == PORT,
                      http.url?.path == url.path else {
                    var detail = "导出失败（HTTP \(http.statusCode)）"
                    if let handle = try? FileHandle(forReadingFrom: temporary) {
                        defer { try? handle.close() }
                        if let data = try? handle.read(upToCount: 4096),
                           let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                           let message = json["detail"] as? String { detail = message }
                    }
                    self.reportDownload(["ok": false, "error": detail])
                    return
                }
                let fm = FileManager.default
                let staging = destination.deletingLastPathComponent()
                    .appendingPathComponent(".fennec-export-" + UUID().uuidString)
                do {
                    try fm.copyItem(at: temporary, to: staging)
                    if fm.fileExists(atPath: destination.path) {
                        _ = try fm.replaceItemAt(destination, withItemAt: staging)
                    } else {
                        try fm.moveItem(at: staging, to: destination)
                    }
                    self.reportDownload(["ok": true])
                } catch {
                    try? fm.removeItem(at: staging)
                    self.reportDownload(["ok": false, "error": "保存失败：" + error.localizedDescription])
                }
            }.resume()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        loadHTML(errorPage(detail: ns.localizedDescription))
        setStatus("页面加载失败")
    }

    private func loadHTML(_ html: String) {
        showingPlaceholder = true
        webView.loadHTMLString(html, baseURL: server.baseURL)
    }

    // MARK: 占位页

    private func loadingPage() -> String {
        """
        <html><body style="font:15px -apple-system;display:flex;align-items:center;justify-content:center;
        height:100vh;margin:0;background:#1c1c1e;color:#e5e5ea">
        <div style="text-align:center">
        <div style="font-size:17px">正在启动转写服务…</div>
        <div style="margin-top:8px;color:#8e8e93">首次启动需要加载 Whisper 模型，约需十几秒</div>
        </div></body></html>
        """
    }

    private func htmlEscape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }

    private func logTail(_ lines: Int = 15) -> String {
        guard let text = try? String(contentsOfFile: LOG_PATH, encoding: .utf8) else { return "(无日志)" }
        let all = text.split(separator: "\n", omittingEmptySubsequences: true)
        return all.suffix(lines).joined(separator: "\n")
    }

    private func errorPage(detail: String = "") -> String {
        let extra = detail.isEmpty ? "" : "<p style='color:#ff9f0a'>错误：\(htmlEscape(detail))</p>"
        let tail = htmlEscape(logTail()).replacingOccurrences(of: "\n", with: "<br>")
        return """
        <html><body style="font:14px -apple-system;margin:0;background:#1c1c1e;color:#e5e5ea;padding:40px">
        <div style="max-width:760px;margin:0 auto">
        <div style="font-size:17px;margin-bottom:12px">服务启动失败</div>
        <p><a href="fennec://retry" style="color:#0a84ff">点击重试</a></p>
        <p style="color:#8e8e93">项目目录：\(htmlEscape(rootPath))</p>
        <p style="color:#8e8e93">日志：\(htmlEscape(LOG_PATH))</p>
        \(extra)
        <pre style="background:#2c2c2e;padding:12px;border-radius:8px;overflow:auto;
        font:11px 'SF Mono',Menlo,monospace;color:#d1d1d6;white-space:pre-wrap">\(tail)</pre>
        </div></body></html>
        """
    }
}

/// 供信号处理器访问（SIGTERM / SIGINT 时也要干净地停掉后端）
private weak var gDelegate: AppDelegate?

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
gDelegate = delegate

// 不在 POSIX handler 中分配 Swift 对象，也不在 main GCD block 中调用 terminate。
// AppKit 的 terminateLater 会跑嵌套事件循环；main queue 不重入时，保存回调会死等。
private var terminationSignals: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: DispatchQueue.global(qos: .utility))
    source.setEventHandler {
        CFRunLoopPerformBlock(CFRunLoopGetMain(), CFRunLoopMode.commonModes.rawValue) {
            gDelegate?.stopServerAndExit()
        }
        CFRunLoopWakeUp(CFRunLoopGetMain())
    }
    source.resume()
    terminationSignals.append(source)
}

app.run()
