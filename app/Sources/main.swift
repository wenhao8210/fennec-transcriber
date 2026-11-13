import AppKit
import Darwin
import WebKit

// Wennec 转写 —— macOS 原生外壳
// 职责：托管 uvicorn 后端进程（随 App 启动/退出）、提供原生窗口与菜单栏常驻入口。

let PORT = 8778
let LOG_PATH = NSHomeDirectory() + "/Library/Logs/Fennec/server.log"
let FENNEC_DATA_DIR = NSHomeDirectory() + "/Library/Application Support/com.kkapp.fennec/datas"

// MARK: - 路径

func projectRoot() -> String {
    if let env = ProcessInfo.processInfo.environment["FENNEC_HOME"], !env.isEmpty { return env }
    let root = Bundle.main.bundleURL.deletingLastPathComponent().path
    if FileManager.default.fileExists(atPath: root + "/backend/main.py") { return root }
    // Keep the built app beside backend/, or set FENNEC_HOME explicitly.
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

    /// 服务是否真正可用（HTTP GET /api/sessions 返回 2xx）
    /// 比 TCP 探活严格：端口绑定后 FastAPI 还要 init_db + 扫描录音目录，这段时间页面请求会挂住
    func isServing() -> Bool {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(PORT)/api/sessions")!)
        req.httpMethod = "GET"
        req.timeoutInterval = 8
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse { ok = (200..<400).contains(http.statusCode) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 10)
        return ok
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

    /// 强制重启（占端口的进程一并清掉，无论是不是本 App 起的）
    func restart() {
        killPortOwner()
        process = nil
        Thread.sleep(forTimeInterval: 1)
        lastStartAttempt = .distantPast
        start()
    }

    /// 占用 8778 的 Fennec 服务进程（只认 main:app，避免误杀别的程序）
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
            if isFennecServer(pid) { pids.append(pid) }
        }
        return pids
    }

    private func isFennecServer(_ pid: Int32) -> Bool {
        let ps = Process()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps")
        ps.arguments = ["-o", "command=", "-p", String(pid)]
        let pipe = Pipe()
        ps.standardOutput = pipe
        try? ps.run()
        ps.waitUntilExit()
        let cmd = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return cmd.contains("main:app")
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

    /// 停止服务：先停自己起的进程，再清掉接管来的外部进程，保证退出后端口释放
    func stop() {
        wantsServer = false
        if let p = process, p.isRunning {
            p.terminationHandler = nil
            p.terminate()
            let deadline = Date().addingTimeInterval(4)
            while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
        process = nil
        killPortOwner()
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var statusMenuItem: NSMenuItem!
    private var server: ServerManager!
    private var rootPath = ""
    private var healthTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        rootPath = projectRoot()
        server = ServerManager(root: rootPath)

        NSApp.setActivationPolicy(.regular)
        buildMainMenu()
        buildStatusItem()
        buildWindow()

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

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

    /// 被信号（kill / Ctrl-C）结束时调用
    func stopServerAndExit() {
        healthTimer?.invalidate()
        server.stop()
        exit(0)
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
            if !ok && retry == 0 {
                self.server.restart()
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
                self.setStatus("服务已停止，正在重启…")
                self.booting = true
                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                    guard let self else { return }
                    self.server.restart()
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
        win.title = "Wennec 转写"
        win.minSize = NSSize(width: 900, height: 600)
        win.center()
        win.delegate = self
        win.isReleasedWhenClosed = false
        win.setFrameAutosaveName("FennecMainWindow")

        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []
        // 前端复制走原生剪贴板：WKWebView 里 navigator.clipboard / execCommand
        // 在异步取数后已失去用户手势，必然失败
        config.userContentController.add(self, name: "hubCopy")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        let wv = WKWebView(frame: .zero, configuration: config)
        wv.navigationDelegate = self
        wv.allowsBackForwardNavigationGestures = true  // 兜底：万一页面被导航走，可用双指滑动/手势返回
        wv.autoresizingMask = [.width, .height]
        win.contentView = wv

        window = win
        webView = wv
    }

    // MARK: 云端转写

    /// ⌘T：等价于点击页面上的「云端转写」按钮。
    /// 走前端按钮而不是直接调接口，这样按钮的禁用态（未选中 / 转写中）一并复用。
    @objc func cloudTranscribe() {
        showMainWindow()
        let js = """
        (() => {
          const b = document.getElementById('btnCloud');
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
                    print("云端转写失败：\(error)")
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

    private func menuItem(_ title: String, action: Selector?, key: String = "", target: AnyObject? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = action == nil ? nil : (target ?? self)
        return item
    }

    private func appMenuItems() -> [NSMenuItem] {
        [
            menuItem("打开主界面", action: #selector(showMainWindow), key: "o"),
            menuItem("云端转写（阿里 Paraformer）", action: #selector(cloudTranscribe), key: "t"),
            menuItem("重新加载页面", action: #selector(reloadPage), key: "r"),
            menuItem("在浏览器中打开", action: #selector(openInBrowser), key: "b"),
            .separator(),
            menuItem("重启服务", action: #selector(restartServer)),
            menuItem("打开录音目录", action: #selector(openRecordingsDir)),
            menuItem("打开转写数据目录", action: #selector(openDataDir)),
            menuItem("在 Finder 中显示日志", action: #selector(revealLog)),
        ]
    }

    private func buildMainMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(menuItem("关于 Wennec", action: #selector(showAbout)))
        appMenu.addItem(.separator())
        appMenuItems().forEach { appMenu.addItem($0) }
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
        menu.addItem(menuItem("打开主界面", action: #selector(showMainWindow)))
        statusMenuItem = menuItem("状态：启动中…", action: nil)
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())
        appMenuItems().forEach { menu.addItem($0) }
        menu.addItem(.separator())
        menu.addItem(menuItem("关于 Wennec", action: #selector(showAbout)))
        menu.addItem(menuItem("退出 Wennec", action: #selector(NSApplication.terminate(_:)), key: "q", target: NSApp))
        statusItem.menu = menu
    }

    // MARK: 动作

    @objc private func showMainWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func reloadPage() {
        webView.load(URLRequest(url: server.baseURL))
        showingPlaceholder = false
    }

    @objc private func openInBrowser() {
        NSWorkspace.shared.open(server.baseURL)
    }

    @objc private func restartServer() {
        boot(retry: 0)   // 允许失败后杀掉旧进程再起
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
        let credits = NSMutableAttributedString(string: "Wennec 录音本地转写\n本地 Whisper + 阿里云云端转写\n数据目录：\(rootPath)/data")
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Wennec 转写",
            .applicationVersion: "1.0.0",
            .credits: credits,
        ])
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if ["wennec", "fennec"].contains(navigationAction.request.url?.scheme?.lowercased() ?? "") {
            decisionHandler(.cancel)
            boot(retry: 0)
            return
        }
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.absoluteString == "about:blank"
            || (url.scheme == "http"
                && ["127.0.0.1", "localhost"].contains(url.host ?? "")
                && url.port == PORT) {
            decisionHandler(.allow)
            return
        }
        if ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
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
              origin.protocol == "http",
              ["127.0.0.1", "localhost"].contains(origin.host),
              origin.port == PORT,
              message.name == "hubCopy", let text = message.body as? String else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
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
        <p><a href="wennec://retry" style="color:#0a84ff">点击重试</a></p>
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

// kill / Ctrl-C 时也优雅退出，避免留下孤儿 uvicorn 进程
for sig in [SIGTERM, SIGINT] {
    signal(sig) { _ in
        DispatchQueue.main.async {
            gDelegate?.stopServerAndExit()
        }
    }
}

app.run()
