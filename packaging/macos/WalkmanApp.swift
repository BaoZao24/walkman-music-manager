import AppKit
import Foundation
import WebKit

final class WalkmanAppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var startupView: StartupView!
    private var serverProcess: Process?
    private var serverLogHandle: FileHandle?
    private var readyFileURL: URL?
    private var readinessTimer: Timer?
    private var isCheckingHealth = false
    private var didLoadInterface = false
    private var launchStartedAt = Date()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        installApplicationMenu()
        makeWindow()
        startBackend()
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        readinessTimer?.invalidate()
        if let readyFileURL {
            try? FileManager.default.removeItem(at: readyFileURL)
        }
        if let serverProcess, serverProcess.isRunning {
            serverProcess.terminate()
        }
        try? serverLogHandle?.close()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func installApplicationMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "退出 Walkman Music Manager",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)
        NSApplication.shared.mainMenu = mainMenu
    }

    private func makeWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1180, height: 800)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Walkman Music Manager"
        window.minSize = NSSize(width: 820, height: 600)
        window.center()

        let configuration = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]

        startupView = StartupView(frame: frame)
        startupView.onRetry = { [weak self] in self?.retryBackend() }
        window.contentView = startupView
        window.makeKeyAndOrderFront(nil)
    }

    private func startBackend() {
        readinessTimer?.invalidate()
        didLoadInterface = false
        isCheckingHealth = false
        launchStartedAt = Date()
        startupView.showLoading("正在启动本机音乐管理服务…")

        let helperURL = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Helpers/WalkmanServer")
        let readyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("walkman-\(UUID().uuidString).url")
        readyFileURL = readyURL

        do {
            let logDirectory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs/Walkman Music Manager", isDirectory: true)
            try FileManager.default.createDirectory(
                at: logDirectory,
                withIntermediateDirectories: true
            )
            let logURL = logDirectory.appendingPathComponent("server.log")
            if !FileManager.default.createFile(atPath: logURL.path, contents: nil) {
                _ = try? FileManager.default.attributesOfItem(atPath: logURL.path)
            }
            let logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle.truncate(atOffset: 0)

            let process = Process()
            process.executableURL = helperURL
            process.arguments = [
                "--host", "127.0.0.1",
                "--port", "0",
                "--ready-file", readyURL.path,
            ]
            process.environment = ProcessInfo.processInfo.environment.merging(
                ["PYTHONUNBUFFERED": "1"],
                uniquingKeysWith: { _, newValue in newValue }
            )
            process.standardOutput = logHandle
            process.standardError = logHandle
            process.terminationHandler = { [weak self] terminatedProcess in
                DispatchQueue.main.async {
                    guard let self, !self.didLoadInterface else { return }
                    self.readinessTimer?.invalidate()
                    self.startupView.showFailure(
                        "本机服务已退出（代码 \(terminatedProcess.terminationStatus)）。"
                    )
                }
            }
            try process.run()
            serverLogHandle = logHandle
            serverProcess = process
            readinessTimer = Timer.scheduledTimer(
                withTimeInterval: 0.25,
                repeats: true
            ) { [weak self] _ in
                self?.checkBackendReadiness()
            }
        } catch {
            startupView.showFailure("本机服务启动失败：\(error.localizedDescription)")
        }
    }

    private func checkBackendReadiness() {
        guard !didLoadInterface, !isCheckingHealth else { return }
        guard let serverProcess, serverProcess.isRunning else {
            readinessTimer?.invalidate()
            startupView.showFailure("本机服务没有正常启动。")
            return
        }
        if Date().timeIntervalSince(launchStartedAt) > 30 {
            readinessTimer?.invalidate()
            startupView.showFailure("本机服务启动超时。请重试，或查看用户日志目录中的 server.log。")
            return
        }
        guard
            let readyFileURL,
            let address = try? String(contentsOf: readyFileURL, encoding: .utf8),
            let baseURL = URL(string: address.trimmingCharacters(in: .whitespacesAndNewlines))
        else {
            return
        }

        isCheckingHealth = true
        let healthURL = baseURL.appendingPathComponent("api/health")
        var request = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData)
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isCheckingHealth = false
                guard !self.didLoadInterface else { return }
                let payload = (try? JSONSerialization.jsonObject(with: data ?? Data())) as? [String: Any]
                let healthy = (response as? HTTPURLResponse)?.statusCode == 200
                    && (payload?["ok"] as? Bool) == true
                guard healthy else { return }

                self.didLoadInterface = true
                self.readinessTimer?.invalidate()
                let contentBounds = self.window.contentView?.bounds ?? .zero
                self.webView.frame = contentBounds
                self.window.contentView = self.webView
                self.webView.load(URLRequest(url: baseURL))
                if let readyFileURL = self.readyFileURL {
                    try? FileManager.default.removeItem(at: readyFileURL)
                    self.readyFileURL = nil
                }
            }
        }.resume()
    }

    private func retryBackend() {
        readinessTimer?.invalidate()
        if let serverProcess, serverProcess.isRunning {
            serverProcess.terminate()
            serverProcess.waitUntilExit()
        }
        self.serverProcess = nil
        startBackend()
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled else { return }
        window.contentView = startupView
        startupView.showFailure("界面加载失败：\(error.localizedDescription)")
    }
}

private final class StartupView: NSView {
    var onRetry: (() -> Void)?

    private let messageLabel = NSTextField(labelWithString: "")
    private let spinner = NSProgressIndicator()
    private let retryButton = NSButton(title: "重新连接", target: nil, action: nil)

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.white.cgColor
        buildLayout()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showLoading(_ message: String) {
        messageLabel.stringValue = message
        spinner.isHidden = false
        spinner.startAnimation(nil)
        retryButton.isHidden = true
    }

    func showFailure(_ message: String) {
        messageLabel.stringValue = message
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        retryButton.isHidden = false
    }

    private func buildLayout() {
        let mark = NSView()
        mark.translatesAutoresizingMaskIntoConstraints = false
        mark.wantsLayer = true
        mark.layer?.backgroundColor = NSColor(red: 0.98, green: 0.82, blue: 0.12, alpha: 1).cgColor

        let markText = NSTextField(labelWithString: "W")
        markText.translatesAutoresizingMaskIntoConstraints = false
        markText.font = .systemFont(ofSize: 24, weight: .black)
        markText.textColor = .black
        mark.addSubview(markText)

        let titleLabel = NSTextField(labelWithString: "Walkman Music Manager")
        titleLabel.translatesAutoresizingMaskIntoConstraints = false
        titleLabel.font = .systemFont(ofSize: 22, weight: .semibold)
        titleLabel.textColor = .black

        messageLabel.translatesAutoresizingMaskIntoConstraints = false
        messageLabel.font = .systemFont(ofSize: 13)
        messageLabel.textColor = NSColor(white: 0.38, alpha: 1)
        messageLabel.maximumNumberOfLines = 2
        messageLabel.alignment = .center

        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.isIndeterminate = true

        retryButton.translatesAutoresizingMaskIntoConstraints = false
        retryButton.bezelStyle = .rounded
        retryButton.target = self
        retryButton.action = #selector(didTapRetry)
        retryButton.isHidden = true

        [mark, titleLabel, messageLabel, spinner, retryButton].forEach(addSubview)

        NSLayoutConstraint.activate([
            mark.centerXAnchor.constraint(equalTo: centerXAnchor),
            mark.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -94),
            mark.widthAnchor.constraint(equalToConstant: 48),
            mark.heightAnchor.constraint(equalToConstant: 48),
            markText.centerXAnchor.constraint(equalTo: mark.centerXAnchor),
            markText.centerYAnchor.constraint(equalTo: mark.centerYAnchor, constant: -1),

            titleLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            titleLabel.topAnchor.constraint(equalTo: mark.bottomAnchor, constant: 20),

            messageLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            messageLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 12),
            messageLabel.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 32),
            messageLabel.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -32),

            spinner.centerXAnchor.constraint(equalTo: centerXAnchor),
            spinner.topAnchor.constraint(equalTo: messageLabel.bottomAnchor, constant: 20),

            retryButton.centerXAnchor.constraint(equalTo: centerXAnchor),
            retryButton.topAnchor.constraint(equalTo: messageLabel.bottomAnchor, constant: 18),
        ])
        spinner.startAnimation(nil)
    }

    @objc private func didTapRetry() {
        onRetry?()
    }
}

@main
struct WalkmanApp {
    static func main() {
        let application = NSApplication.shared
        let delegate = WalkmanAppDelegate()
        application.delegate = delegate
        application.run()
    }
}
